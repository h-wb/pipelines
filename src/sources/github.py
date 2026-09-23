"""GitHub personal activity source.

Mirrors one user's own GitHub footprint: repos, stars, the public/private
event feed (GitHub only keeps ~90 days / 300 events, so run this often),
the contribution calendar, and authored commits / pull requests / issues.
"""

import time
from typing import Any, Iterator, List, Tuple

import dlt
from dlt.common.pendulum import pendulum
from dlt.common.typing import TDataItems
from dlt.extract import DltResource
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth
from dlt.sources.helpers.rest_client.client import RESTClient
from dlt.sources.helpers.rest_client.paginators import HeaderLinkPaginator

API_URL = "https://api.github.com"
SEARCH_CAP = 1000  # the search API never returns more than this per query
SEARCH_PAUSE_S = 2.1  # search is limited to 30 requests / minute
OVERLAP = pendulum.duration(days=3)  # re-fetch window so late updates are merged


def _iso(dt: Any) -> str:
    """UTC timestamp in the `2020-01-31T12:00:00Z` form search qualifiers expect."""
    return dt.in_timezone("UTC").format("YYYY-MM-DD[T]HH:mm:ss[Z]")


@dlt.source(name="github")
def github_source(
    username: str = dlt.config.value,
    access_token: str = dlt.secrets.value,
) -> Iterator[DltResource]:
    """GitHub activity for `username`, authenticated as that user."""
    client = RESTClient(
        base_url=API_URL,
        auth=BearerTokenAuth(access_token),
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        paginator=HeaderLinkPaginator(),
    )
    me = client.get("/user")
    me.raise_for_status()
    account_created = pendulum.parse(me.json()["created_at"])

    def search(path: str, qualifier: str, start: Any, end: Any) -> Iterator[dict]:
        """Yield every search hit for `qualifier:start..end`, splitting the
        range in half whenever it holds more than the 1000-result cap."""
        ranges: List[Tuple[Any, Any]] = [(start, end)]
        while ranges:
            lo, hi = ranges.pop()
            window = f"{qualifier}:{_iso(lo)}..{_iso(hi)}"
            params = {"q": f"author:{username} {window}", "per_page": 100}
            first = True
            for page in client.paginate(path, params=params, data_selector="$"):
                time.sleep(SEARCH_PAUSE_S)
                if first:
                    first = False
                    if page[0]["total_count"] > SEARCH_CAP and hi - lo > pendulum.duration(hours=1):
                        mid = lo + (hi - lo) / 2
                        ranges += [(lo, mid), (mid, hi)]
                        break
                yield from page[0]["items"]

    def since(state_key: str) -> Any:
        """Start of the next incremental window (full history on first run)."""
        last = dlt.current.resource_state().get(state_key)
        return pendulum.parse(last) - OVERLAP if last else account_created

    @dlt.resource(write_disposition="merge", primary_key="id")
    def repos() -> TDataItems:
        for page in client.paginate(
            "/user/repos",
            params={"per_page": 100, "affiliation": "owner,collaborator,organization_member"},
        ):
            yield page

    @dlt.resource(write_disposition="merge", primary_key="repo_id")
    def stars() -> TDataItems:
        # merge (not replace) so a later unstar keeps the history row
        for page in client.paginate(
            "/user/starred",
            params={"per_page": 100},
            headers={"Accept": "application/vnd.github.star+json"},
        ):
            yield [
                {
                    "repo_id": s["repo"]["id"],
                    "full_name": s["repo"]["full_name"],
                    "starred_at": s["starred_at"],
                    "description": s["repo"]["description"],
                    "language": s["repo"]["language"],
                    "stargazers_count": s["repo"]["stargazers_count"],
                    "html_url": s["repo"]["html_url"],
                    "topics": s["repo"].get("topics") or [],
                }
                for s in page
            ]

    @dlt.resource(
        write_disposition="merge",
        primary_key="id",
        # payload shape differs per event type; keep it as one json column
        columns={"payload": {"data_type": "json"}},
    )
    def events() -> TDataItems:
        for page in client.paginate(f"/users/{username}/events", params={"per_page": 100}):
            yield page

    @dlt.resource(write_disposition="merge", primary_key="date")
    def contributions_daily() -> TDataItems:
        start = since("last_date")
        now = pendulum.now("UTC")
        query = """
        query($login: String!, $from: DateTime!, $to: DateTime!) {
          user(login: $login) {
            contributionsCollection(from: $from, to: $to) {
              contributionCalendar {
                weeks { contributionDays { date contributionCount } }
              }
            }
          }
        }"""
        rows = []
        # contributionsCollection spans at most one year per query
        while start < now:
            end = min(start.add(years=1).subtract(seconds=1), now)
            response = client.post(
                "/graphql",
                json={
                    "query": query,
                    "variables": {"login": username, "from": start.isoformat(), "to": end.isoformat()},
                },
            )
            response.raise_for_status()
            resp = response.json()
            if "errors" in resp:
                raise RuntimeError(f"GitHub GraphQL error: {resp['errors']}")
            weeks = resp["data"]["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
            rows += [
                {"date": d["date"], "contribution_count": d["contributionCount"]}
                for w in weeks
                for d in w["contributionDays"]
            ]
            start = end.add(seconds=1)
        yield rows
        if rows:
            dlt.current.resource_state()["last_date"] = max(r["date"] for r in rows)

    @dlt.resource(write_disposition="merge", primary_key="sha")
    def commits() -> TDataItems:
        # the search API only indexes default branches
        run_started = pendulum.now("UTC")
        for c in search("/search/commits", "author-date", since("last_run"), run_started):
            yield {
                "sha": c["sha"],
                "repository": c["repository"]["full_name"],
                "repository_private": c["repository"]["private"],
                "authored_at": c["commit"]["author"]["date"],
                "committed_at": c["commit"]["committer"]["date"],
                "message": c["commit"]["message"],
                "parent_count": len(c["parents"]),
                "html_url": c["html_url"],
            }
        dlt.current.resource_state()["last_run"] = run_started.isoformat()

    def issue_like(kind: str, state_key: str) -> Iterator[dict]:
        # `updated` (not `created`) so state changes like merges get re-fetched
        run_started = pendulum.now("UTC")
        for i in search("/search/issues", f"type:{kind} updated", since(state_key), run_started):
            pr = i.get("pull_request") or {}
            yield {
                "id": i["id"],
                "number": i["number"],
                "repository": i["repository_url"].removeprefix(f"{API_URL}/repos/"),
                "title": i["title"],
                "state": i["state"],
                "state_reason": i.get("state_reason"),
                "draft": i.get("draft"),
                "created_at": i["created_at"],
                "updated_at": i["updated_at"],
                "closed_at": i["closed_at"],
                "merged_at": pr.get("merged_at"),
                "comments": i["comments"],
                "labels": [label["name"] for label in i["labels"]],
                "html_url": i["html_url"],
            }
        dlt.current.resource_state()[state_key] = run_started.isoformat()

    @dlt.resource(write_disposition="merge", primary_key="id")
    def pull_requests() -> TDataItems:
        yield from issue_like("pr", "last_run")

    @dlt.resource(write_disposition="merge", primary_key="id")
    def issues() -> TDataItems:
        yield from issue_like("issue", "last_run")

    return (
        repos,
        stars,
        events,
        contributions_daily,
        commits,
        pull_requests,
        issues,
    )
