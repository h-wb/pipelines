"""Extraction-only parent for activity in owned repositories."""

import dlt
from dlt.common.typing import TDataItems

from src.integrations.github.client import GitHubClient


@dlt.resource(selected=False)
def owned_repositories(api: GitHubClient) -> TDataItems:
    for name in api.owned_repos:
        yield {"full_name": name}
