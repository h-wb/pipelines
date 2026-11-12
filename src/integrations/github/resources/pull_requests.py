"""GitHub pull requests resource."""

import dlt
from dlt.common.typing import TDataItems
from dlt.sources import incremental

from src.integrations.github.client import GitHubClient, parse_datetime
from src.integrations.github.incremental import (
    INITIAL_TIMESTAMP,
    LAG_SECONDS,
    resume_cursor,
)


@dlt.transformer(write_disposition="merge", primary_key="html_url")
def pull_requests(
    repo: dict[str, str],
    api: GitHubClient,
    updated_at: incremental[str] = incremental(  # noqa: B008
        "updated_at", initial_value=INITIAL_TIMESTAMP, lag=LAG_SECONDS
    ),
) -> TDataItems:
    start = max(
        parse_datetime(resume_cursor(updated_at, api.cursor_starts)),
        api.account_created,
    )
    # Ordering is per repository, not across the whole transformer stream.
    params = {"state": "all", "sort": "updated", "direction": "desc", "per_page": 100}
    for page in api.pages(f"/repos/{repo['full_name']}/pulls", params=params):
        fresh = [p for p in page if parse_datetime(p["updated_at"]) >= start]
        stop = len(fresh) < len(page)
        yield fresh
        if stop:
            break
