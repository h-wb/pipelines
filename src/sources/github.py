"""GitHub personal activity source (extract only; transforms live in dbt).

Account level: repos, stars, the event feed (GitHub keeps only ~90 days /
300 events, so run this often) and the contribution calendar.

Repo level, for every repo I own (forks included, any author, bots included):
commits on every branch (with line stats), pull requests, issues, issue
comments and releases. Plus `external_*`: my commits and issues/PRs in other
people's repos, from the search API.

Records are yielded as the API returns them; dlt flattens them. The only
logic here is incremental windows and pagination.

`export_dir` seeds history once from a GitHub "Export account data" archive
into raw `export_*` tables (its JSON records, plus `git log` of the bundled
repos). The API resources then start from the export date, and dbt unions
the two on the web URL.
"""

import glob
import json
import os
import subprocess
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import dlt
from dlt.common.pendulum import pendulum
from dlt.common.typing import TDataItems
from dlt.extract import DltResource
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth
from dlt.sources.helpers.rest_client.client import RESTClient
from dlt.sources.helpers.rest_client.paginators import HeaderLinkPaginator
from requests import HTTPError

API_URL = "https://api.github.com"
SEARCH_CAP = 1000  # the search API never returns more than this per query
SEARCH_PAUSE_S = 2.1  # search is limited to 30 requests / minute
OVERLAP = pendulum.duration(days=3)  # re-fetch window so late updates are merged

REPO_RESOURCES = ("commits", "pull_requests", "issues", "issue_comments", "releases",
                  "external_commits", "external_issues")
EXPORT_RESOURCES = ("export_commits", "export_pull_requests", "export_issues",
                    "export_issue_comments", "export_releases", "export_bots")
EXPORT_KINDS = ("pull_requests", "issues", "issue_comments", "releases", "bots")


def _iso(dt: Any) -> str:
    """UTC timestamp in the `2020-01-31T12:00:00Z` form GitHub expects."""
    return dt.in_timezone("UTC").format("YYYY-MM-DD[T]HH:mm:ss[Z]")


def _load_export(export_dir: str, kind: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(export_dir, f"{kind}_*.json"))):
        with open(path) as f:
            rows += json.load(f)
    return rows


def _git(git_dir: str, *args: str) -> str:
    return subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        capture_output=True, text=True, check=True, errors="replace",
    ).stdout


@dlt.source(name="github")
def github_source(
    username: str = dlt.config.value,
    access_token: str = dlt.secrets.value,
    export_dir: Optional[str] = None,
) -> Iterator[DltResource]:
    """GitHub activity for `username`, authenticated as that user.

    Args:
        export_dir: extracted account-data export; enables the `export_*`
            resources and starts the repo-level API resources at its date
    """
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

    # where the repo-level API resources start on their first run
    repo_start = account_created
    if export_dir:
        repo_start = max(
            pendulum.parse(r["created_at"])
            for kind in ("pull_requests", "issues", "issue_comments")
            for r in _load_export(export_dir, kind)
        )

    def since(state_key: str = "last_run", first: Any = None) -> Any:
        """Start of the next incremental window."""
        last = dlt.current.resource_state().get(state_key)
        return pendulum.parse(last) - OVERLAP if last else (first or account_created)

    def mark(at: Any, state_key: str = "last_run") -> None:
        dlt.current.resource_state()[state_key] = at.isoformat()

    _owned: List[str] = []

    def owned_repos() -> List[str]:
        if not _owned:
            _owned.extend(
                r["full_name"]
                for page in client.paginate("/user/repos", params={"per_page": 100, "affiliation": "owner"})
                for r in page
            )
        return _owned

    def search(path: str, query: str, qualifier: str, start: Any, end: Any) -> Iterator[dict]:
        """Yield every search hit for `query qualifier:start..end`, splitting the
        range in half whenever it holds more than the 1000-result cap."""
        ranges: List[Tuple[Any, Any]] = [(start, end)]
        while ranges:
            lo, hi = ranges.pop()
            params = {"q": f"{query} {qualifier}:{_iso(lo)}..{_iso(hi)}", "per_page": 100}
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

    def commit_with_stats(repo: str, sha: str) -> Dict[str, Any]:
        """Single-commit endpoint: the list endpoints have no line stats."""
        response = client.get(f"/repos/{repo}/commits/{sha}")
        response.raise_for_status()
        commit = response.json()
        commit.pop("files", None)  # per-file patches: the full diff, far too big
        return commit

    # --- account level -----------------------------------------------------------

    @dlt.resource(write_disposition="merge", primary_key="id")
    def repos() -> TDataItems:
        for page in client.paginate(
            "/user/repos",
            params={"per_page": 100, "affiliation": "owner,collaborator,organization_member"},
        ):
            yield page

    @dlt.resource(write_disposition="merge", primary_key="repo__id")
    def stars() -> TDataItems:
        # merge (not replace) so a later unstar keeps the history row
        for page in client.paginate(
            "/user/starred",
            params={"per_page": 100},
            headers={"Accept": "application/vnd.github.star+json"},
        ):
            yield page

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
              contributionCalendar { weeks { contributionDays { date contributionCount } } }
            }
          }
        }"""
        days: List[Dict[str, Any]] = []
        # contributionsCollection spans at most one year per query
        while start < now:
            end = min(start.add(years=1).subtract(seconds=1), now)
            response = client.post(
                "/graphql",
                json={"query": query, "variables": {"login": username, "from": start.isoformat(), "to": end.isoformat()}},
            )
            response.raise_for_status()
            body = response.json()
            if "errors" in body:
                raise RuntimeError(f"GitHub GraphQL error: {body['errors']}")
            for week in body["data"]["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]:
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
                branches = [b["name"] for page in client.paginate(f"/repos/{repo}/branches", params={"per_page": 100}) for b in page]
            except HTTPError as e:
                if e.response is not None and e.response.status_code == 409:
                    continue  # empty repository
                raise
            seen = set()  # a commit shows up on every branch that contains it
            for branch in branches:
                for page in client.paginate(f"/repos/{repo}/commits", params={"sha": branch, "since": start, "per_page": 100}):
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
            params = {"state": "all", "sort": "updated", "direction": "desc", "per_page": 100}
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
        params = {"state": "all", "since": _iso(since(first=repo_start)), "per_page": 100}
        for repo in owned_repos():
            for page in client.paginate(f"/repos/{repo}/issues", params=params):
                yield page
        mark(run_started)

    @dlt.resource(write_disposition="merge", primary_key="html_url")
    def issue_comments() -> TDataItems:
        run_started = pendulum.now("UTC")
        params = {"since": _iso(since(first=repo_start)), "sort": "updated", "direction": "asc", "per_page": 100}
        for repo in owned_repos():
            for page in client.paginate(f"/repos/{repo}/issues/comments", params=params):
                yield page
        mark(run_started)

    @dlt.resource(write_disposition="merge", primary_key="html_url")
    def releases() -> TDataItems:
        # small enough to re-read in full every night
        for repo in owned_repos():
            for page in client.paginate(f"/repos/{repo}/releases", params={"per_page": 100}):
                yield page

    # --- my activity in other people's repos (search) ------------------------------

    external = f"author:{username} -user:{username}"

    @dlt.resource(write_disposition="merge", primary_key="html_url")
    def external_commits() -> TDataItems:
        # the search only indexes default branches
        run_started = pendulum.now("UTC")
        for c in search("/search/commits", external, "author-date", since(), run_started):
            yield commit_with_stats(c["repository"]["full_name"], c["sha"])
        mark(run_started)

    @dlt.resource(write_disposition="merge", primary_key="html_url")
    def external_issues() -> TDataItems:
        """Issues and PRs (search returns both as issues)."""
        run_started = pendulum.now("UTC")
        start = since()
        # issue search requires the type qualifier
        for kind in ("is:issue", "is:pull-request"):
            yield from search("/search/issues", f"{kind} {external}", "updated", start, run_started)
        mark(run_started)

    resources = [repos, stars, events, contributions_daily, commits, pull_requests,
                 issues, issue_comments, releases, external_commits, external_issues]

    # --- one-shot export seed (raw) ------------------------------------------------

    if export_dir:
        def export_resource(kind: str) -> DltResource:
            return dlt.resource(
                _load_export(export_dir, kind),
                name=f"export_{kind}",
                write_disposition="replace",
                primary_key="url",
            )

        @dlt.resource(write_disposition="replace", primary_key=("repository", "sha"))
        def export_commits() -> TDataItems:
            """`git log --all` of every repo bundled in the export, with --numstat totals."""
            fields = ["sha", "author_name", "author_email", "authored_at", "committer_name",
                      "committer_email", "committed_at", "parents", "message"]
            fmt = "\x1f".join(["%H", "%an", "%ae", "%aI", "%cn", "%ce", "%cI", "%P", "%B"]) + "\x1e"
            for git_dir in sorted(glob.glob(os.path.join(export_dir, "repositories", "*", "*.git"))):
                repo = f"{os.path.basename(os.path.dirname(git_dir))}/{os.path.basename(git_dir)[:-4]}"
                stats: Dict[str, Dict[str, int]] = {}
                sha = None
                for line in _git(git_dir, "log", "--all", "--numstat", "--format=%x00%H").splitlines():
                    if line.startswith("\x00"):
                        sha = line[1:]
                        stats[sha] = {"additions": 0, "deletions": 0, "files_changed": 0}
                    elif line.strip() and sha:
                        added, deleted, _ = line.split("\t", 2)
                        stats[sha]["files_changed"] += 1
                        # binary files show "-" for both counts
                        stats[sha]["additions"] += int(added) if added.isdigit() else 0
                        stats[sha]["deletions"] += int(deleted) if deleted.isdigit() else 0
                rows = []
                for record in _git(git_dir, "log", "--all", f"--format={fmt}").split("\x1e"):
                    values = record.strip("\n").split("\x1f")
                    if len(values) == len(fields):
                        row = dict(zip(fields, values))
                        rows.append({"repository": repo, **row, **stats.get(row["sha"], {})})
                yield rows

        resources += [export_commits] + [export_resource(kind) for kind in EXPORT_KINDS]

    return resources
