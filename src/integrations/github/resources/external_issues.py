"""GitHub external issues resource."""

import dlt
from dlt.common.pendulum import pendulum
from dlt.common.typing import TDataItems

from src.integrations.github.client import GitHubClient


@dlt.resource(write_disposition="merge", primary_key="html_url")
def external_issues(api: GitHubClient) -> TDataItems:
    """Issues and PRs (search returns both as issues)."""
    run_started = pendulum.now("UTC")
    start = api.since()
    # issue search requires the type qualifier
    for kind in ("is:issue", "is:pull-request"):
        yield from api.search(
            "/search/issues",
            f"{kind} {api.external_query}",
            "updated",
            start,
            run_started,
        )
    api.mark(run_started)
