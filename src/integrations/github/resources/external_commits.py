"""GitHub external commits resource."""

import dlt
from dlt.common.pendulum import pendulum
from dlt.common.typing import TDataItems

from src.integrations.github.client import GitHubClient


@dlt.resource(write_disposition="merge", primary_key="html_url")
def external_commits(api: GitHubClient) -> TDataItems:
    # the search only indexes default branches
    run_started = pendulum.now("UTC")
    for c in api.search(
        "/search/commits", api.external_query, "author-date", api.since(), run_started
    ):
        commit = api.commit_with_stats(c["repository"]["full_name"], c["sha"])
        if commit is not None:
            yield commit
    api.mark(run_started)
