"""GitHub releases resource."""

import dlt
from dlt.common.typing import TDataItems

from src.integrations.github.client import GitHubClient


@dlt.transformer(write_disposition="merge", primary_key="html_url")
def releases(repo: dict[str, str], api: GitHubClient) -> TDataItems:
    # small enough to re-read in full every night
    yield from api.pages(
        f"/repos/{repo['full_name']}/releases", params={"per_page": 100}
    )
