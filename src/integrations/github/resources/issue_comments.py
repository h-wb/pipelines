"""GitHub issue comments resource."""

import dlt
from dlt.common.typing import TDataItems
from dlt.sources import incremental

from src.integrations.github.client import GitHubClient, parse_datetime
from src.integrations.github.client import iso as _iso
from src.integrations.github.incremental import (
    INITIAL_TIMESTAMP,
    LAG_SECONDS,
    resume_cursor,
)


@dlt.transformer(write_disposition="merge", primary_key="html_url")
def issue_comments(
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
    params = {
        "since": _iso(start),
        "sort": "updated",
        "direction": "asc",
        "per_page": 100,
    }
    yield from api.pages(f"/repos/{repo['full_name']}/issues/comments", params=params)
