"""Compose GitHub activity resources without making network requests."""

import dlt
from dlt.extract import DltResource
from requests import Session

from src.integrations.github.client import GitHubClient
from src.integrations.github.resources.commits import commits
from src.integrations.github.resources.contributions_daily import contributions_daily
from src.integrations.github.resources.events import events
from src.integrations.github.resources.external_commits import external_commits
from src.integrations.github.resources.external_issues import external_issues
from src.integrations.github.resources.issue_comments import issue_comments
from src.integrations.github.resources.issues import issues
from src.integrations.github.resources.owned_repositories import owned_repositories
from src.integrations.github.resources.pull_requests import pull_requests
from src.integrations.github.resources.releases import releases
from src.integrations.github.resources.repos import REPOS
from src.integrations.github.resources.stars import STARS


@dlt.source(name="github")
def github_source(
    username: str, access_token: str, session: Session
) -> list[DltResource]:
    api = GitHubClient(username, access_token, session)
    owned = owned_repositories(api)
    return [
        *api.rest_resources(REPOS, STARS, events(username)),
        contributions_daily(api),
        commits(api),
        owned,
        owned | pull_requests(api=api),
        owned | issues(api=api),
        owned | issue_comments(api=api),
        owned | releases(api=api),
        external_commits(api),
        external_issues(api),
    ]
