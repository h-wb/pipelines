"""GitHub personal activity source (extract only; transforms live in dbt).

Account level: repos, stars, the event feed (GitHub keeps only ~90 days /
300 events, so run this often) and the contribution calendar.

Repo level, for every repo I own (forks included, any author, bots included):
commits on every branch (with line stats), pull requests, issues, issue
comments and releases. Plus `external_*`: my commits and issues/PRs in other
people's repos, from the search API.

Records are yielded as the API returns them; dlt flattens them. The only
logic here is incremental windows and pagination.
"""

import time
from collections.abc import Iterator
from typing import Any

import dlt
from dlt.common.pendulum import pendulum
from dlt.common.typing import TDataItems
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth
from dlt.sources.helpers.rest_client.client import RESTClient
from dlt.sources.helpers.rest_client.paginators import HeaderLinkPaginator
from requests import HTTPError

API_URL = "https://api.github.com"
SEARCH_CAP = 1000  # the search API never returns more than this per query
SEARCH_PAUSE_S = 2.1  # search is limited to 30 requests / minute
OVERLAP = pendulum.duration(days=3)  # re-fetch window so late updates are merged


def _iso(dt: Any) -> str:
    """UTC timestamp in the `2020-01-31T12:00:00Z` form GitHub expects."""
    return str(dt.in_timezone("UTC").format("YYYY-MM-DD[T]HH:mm:ss[Z]"))


@dlt.source(name="github")
def github_source(
    username: str,
    access_token: str,
    session: Any,
) -> list[Any]:
    """GitHub activity for `username`, authenticated as that user."""
    client = RESTClient(
        base_url=API_URL,
        session=session,
        auth=BearerTokenAuth(access_token),
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        paginator=HeaderLinkPaginator(),
    )
    me = client.get("/user")
    me.raise_for_status()
    if me.json().get("login", "").casefold() != username.casefold():
        raise ValueError("GitHub token belongs to a different account")
    account_created = pendulum.parse(me.json()["created_at"])

    # where the repo-level API resources start on their first run
    repo_start = account_created

    def since(state_key: str = "last_run", first: Any = None) -> Any:
        """Start of the next incremental window."""
        last = dlt.current.resource_state().get(state_key)
        return pendulum.parse(last) - OVERLAP if last else (first or account_created)

    def mark(at: Any, state_key: str = "last_run") -> None:
        dlt.current.resource_state()[state_key] = at.isoformat()

    _owned: list[str] = []

    def owned_repos() -> list[str]:
        if not _owned:
            _owned.extend(
                r["full_name"]
                for page in client.paginate(
                    "/user/repos", params={"per_page": 100, "affiliation": "owner"}
                )
                for r in page
            )
        return _owned

    def search(
        path: str, query: str, qualifier: str, start: Any, end: Any
    ) -> Iterator[dict]:
        """Yield every search hit for `query qualifier:start..end`, splitting the
        range in half whenever it holds more than the 1000-result cap."""
        ranges: list[tuple[Any, Any]] = [(start, end)]
        while ranges:
            lo, hi = ranges.pop()
            params = {
                "q": f"{query} {qualifier}:{_iso(lo)}..{_iso(hi)}",
                "per_page": 100,
            }
            first = True
            for page in client.paginate(path, params=params, data_selector="$"):
                time.sleep(SEARCH_PAUSE_S)
                if first:
                    first = False
                    if page[0][
                        "total_count"
                    ] > SEARCH_CAP and hi - lo > pendulum.duration(hours=1):
                        mid = lo + (hi - lo) / 2
                        ranges += [(lo, mid), (mid, hi)]
                        break
                yield from page[0]["items"]

    def commit_with_stats(repo: str, sha: str) -> dict[str, Any]:
        """Single-commit endpoint: the list endpoints have no line stats."""
        response = client.get(f"/repos/{repo}/commits/{sha}")
        response.raise_for_status()
        commit: dict[str, Any] = response.json()
        commit.pop("files", None)  # per-file patches: the full diff, far too big
        return commit

    # --- account level -----------------------------------------------------------

    @dlt.resource(write_disposition="merge", primary_key="id")
    def repos() -> TDataItems:
        yield from client.paginate(
            "/user/repos",
            params={
                "per_page": 100,
                "affiliation": "owner,collaborator,organization_member",
            },
        )

    @dlt.resource(write_disposition="merge", primary_key="repo__id")
    def stars() -> TDataItems:
        # merge (not replace) so a later unstar keeps the history row
        yield from client.paginate(
            "/user/starred",
            params={"per_page": 100},
            headers={"Accept": "application/vnd.github.star+json"},
        )

    @dlt.resource(
        write_disposition="merge",
        primary_key="id",
        # payload shape differs per event type; keep it as one json column
        columns={"payload": {"data_type": "json"}},
    )
    def events() -> TDataItems:
        yield from client.paginate(
            f"/users/{username}/events", params={"per_page": 100}
        )

    @dlt.resource(write_disposition="merge", primary_key="date")
    def contributions_daily() -> TDataItems:
        start = since("last_date")
        now = pendulum.now("UTC")
        query = """
        query($login: String!, $from: DateTime!, $to: DateTime!) {
          user(login: $login) {
            contributionsCollection(from: $from, to: $to) {
              contributionCalendar { weeks { contributionDays { date contributionCount } } }
            }
          }
        }"""
        days: list[dict[str, Any]] = []
        # contributionsCollection spans at most one year per query
        while start < now:
            end = min(start.add(years=1).subtract(seconds=1), now)
            response = client.post(
                "/graphql",
                json={
                    "query": query,
                    "variables": {
                        "login": username,
                        "from": start.isoformat(),
                        "to": end.isoformat(),
                    },
                },
            )
            response.raise_for_status()
            body = response.json()
            if "errors" in body:
                raise RuntimeError(f"GitHub GraphQL error: {body['errors']}")
            for week in body["data"]["user"]["contributionsCollection"][
                "contributionCalendar"
            ]["weeks"]:
                days += week["contributionDays"]
            start = end.add(seconds=1)
        yield days
        if days:
            dlt.current.resource_state()["last_date"] = max(d["date"] for d in days)

    # --- repo level (my repos, any author) ----------------------------------------

    @dlt.resource(write_disposition="merge", primary_key="html_url")
    def commits() -> TDataItems:
        run_started = pendulum.now("UTC")
        start = _iso(since(first=repo_start))
        for repo in owned_repos():
            try:
                branches = [
                    b["name"]
                    for page in client.paginate(
                        f"/repos/{repo}/branches", params={"per_page": 100}
                    )
                    for b in page
                ]
            except HTTPError as e:
                if e.response is not None and e.response.status_code == 409:
                    continue  # empty repository
                raise
            seen = set()  # a commit shows up on every branch that contains it
            for branch in branches:
                for page in client.paginate(
                    f"/repos/{repo}/commits",
                    params={"sha": branch, "since": start, "per_page": 100},
                ):
                    new = [c["sha"] for c in page if c["sha"] not in seen]
                    seen.update(new)
                    yield [commit_with_stats(repo, sha) for sha in new]
        mark(run_started)

    @dlt.resource(write_disposition="merge", primary_key="html_url")
    def pull_requests() -> TDataItems:
        run_started = pendulum.now("UTC")
        start = since(first=repo_start)
        for repo in owned_repos():
            # no `since` on this endpoint: walk newest-updated first, stop past the window
            params = {
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "per_page": 100,
            }
            for page in client.paginate(f"/repos/{repo}/pulls", params=params):
                fresh = [p for p in page if pendulum.parse(p["updated_at"]) >= start]
                yield fresh
                if len(fresh) < len(page):
                    break
        mark(run_started)

    @dlt.resource(write_disposition="merge", primary_key="html_url")
    def issues() -> TDataItems:
        """Issues endpoint; it also lists PRs (as issues), dbt keeps what it needs."""
        run_started = pendulum.now("UTC")
        params = {
            "state": "all",
            "since": _iso(since(first=repo_start)),
            "per_page": 100,
        }
        for repo in owned_repos():
            yield from client.paginate(f"/repos/{repo}/issues", params=params)
        mark(run_started)

    @dlt.resource(write_disposition="merge", primary_key="html_url")
    def issue_comments() -> TDataItems:
        run_started = pendulum.now("UTC")
        params = {
            "since": _iso(since(first=repo_start)),
            "sort": "updated",
            "direction": "asc",
            "per_page": 100,
        }
        for repo in owned_repos():
            yield from client.paginate(f"/repos/{repo}/issues/comments", params=params)
        mark(run_started)

    @dlt.resource(write_disposition="merge", primary_key="html_url")
    def releases() -> TDataItems:
        # small enough to re-read in full every night
        for repo in owned_repos():
            yield from client.paginate(
                f"/repos/{repo}/releases", params={"per_page": 100}
            )

    # --- my activity in other people's repos (search) ------------------------------

    external = f"author:{username} -user:{username}"

    @dlt.resource(write_disposition="merge", primary_key="html_url")
    def external_commits() -> TDataItems:
        # the search only indexes default branches
        run_started = pendulum.now("UTC")
        for c in search(
            "/search/commits", external, "author-date", since(), run_started
        ):
            yield commit_with_stats(c["repository"]["full_name"], c["sha"])
        mark(run_started)

    @dlt.resource(write_disposition="merge", primary_key="html_url")
    def external_issues() -> TDataItems:
        """Issues and PRs (search returns both as issues)."""
        run_started = pendulum.now("UTC")
        start = since()
        # issue search requires the type qualifier
        for kind in ("is:issue", "is:pull-request"):
            yield from search(
                "/search/issues", f"{kind} {external}", "updated", start, run_started
            )
        mark(run_started)

    resources = [
        repos,
        stars,
        events,
        contributions_daily,
        commits,
        pull_requests,
        issues,
        issue_comments,
        releases,
        external_commits,
        external_issues,
    ]

    return resources
