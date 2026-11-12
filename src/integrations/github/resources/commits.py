"""GitHub commits resource."""

import dlt
from dlt.common.pendulum import pendulum
from dlt.common.typing import TDataItems
from requests import HTTPError

from src.integrations.github.client import GitHubClient
from src.integrations.github.client import iso as _iso


@dlt.resource(write_disposition="merge", primary_key="html_url")
def commits(api: GitHubClient) -> TDataItems:
    run_started = pendulum.now("UTC")
    start = _iso(api.since(first=api.account_created))
    for repo in api.owned_repos:
        try:
            branches = [
                b["name"]
                for page in api.pages(
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
            for page in api.pages(
                f"/repos/{repo}/commits",
                params={"sha": branch, "since": start, "per_page": 100},
            ):
                new = [c["sha"] for c in page if c["sha"] not in seen]
                seen.update(new)
                yield [
                    commit
                    for commit in (api.commit_with_stats(repo, sha) for sha in new)
                    if commit is not None
                ]
    api.mark(run_started)
