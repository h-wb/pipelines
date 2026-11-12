"""GitHub account: API fetch. The account-export archive format is planned (decision 021)."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import Field

from src.core.models import StrictModel
from src.integrations import checks
from src.integrations.contracts import (
    CredentialField,
    Fetch,
    IntegrationDefinition,
    ResourceDefinition,
)


class GitHubSettings(StrictModel):
    username: str = Field(
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,38}$",
        title="GitHub username",
        description="The account that owns the access token.",
    )


@contextmanager
def open_fetch(settings: Any, credentials: dict[str, str]) -> Iterator[Any]:
    from dlt.sources.helpers.requests import Client

    from src.integrations.errors import is_rate_limited
    from src.integrations.github.source import github_source

    with Client(
        raise_for_status=False,
        retry_condition=lambda response, error: (
            response is not None and is_rate_limited(response)
        ),
    ).session as session:
        yield github_source(
            username=settings.username,
            access_token=credentials["access_token"],
            session=session,
        )


DEFINITION = IntegrationDefinition(
    name="github",
    title="GitHub",
    settings_model=GitHubSettings,
    credentials=(
        CredentialField(
            "access_token",
            "Personal access token",
            "A token for this GitHub account, with access to the repositories and contribution data you want to import.",
        ),
    ),
    fetch=Fetch(
        "Connect your account",
        "Fetch repository activity and your contribution history from GitHub.",
        open_fetch,
    ),
    resources=(
        ResourceDefinition(
            name="repos",
            label="Repositories",
            description="Owned, collaborated-on, and organization repositories accessible to your token.",
            record_kind="entity",
        ),
        ResourceDefinition(
            name="stars",
            label="Stars",
            description="Repositories you have starred; previously observed stars are retained.",
        ),
        ResourceDefinition(
            name="events",
            label="Public events",
            description="Your public activity feed.",
            warning="GitHub exposes only a limited recent history (about 90 days / 300 events).",
        ),
        ResourceDefinition(
            name="contributions_daily",
            label="Contribution calendar",
            description="Daily contribution counts from GitHub's GraphQL API.",
            record_kind="snapshot",
        ),
        ResourceDefinition(
            name="commits",
            label="Commits",
            description="Commits across every branch of your owned repositories, including line statistics.",
            warning="Initial sync can be slow: each commit needs a detail request.",
        ),
        ResourceDefinition(
            name="pull_requests",
            label="Pull requests",
            description="Pull requests in your owned repositories, by any author.",
            record_kind="entity",
        ),
        ResourceDefinition(
            name="issues",
            label="Issues",
            description="Issues and issue-shaped pull requests in your owned repositories.",
            record_kind="entity",
        ),
        ResourceDefinition(
            name="issue_comments",
            label="Issue comments",
            description="Comments on issues and pull requests in your owned repositories.",
            record_kind="entity",
        ),
        ResourceDefinition(
            name="releases",
            label="Releases",
            description="Releases in your owned repositories.",
            record_kind="entity",
        ),
        ResourceDefinition(
            name="external_commits",
            label="External commits",
            description="Your commits in repositories owned by others, with line statistics.",
            warning="GitHub search indexes default branches only; large searches can be slow.",
        ),
        ResourceDefinition(
            name="external_issues",
            label="External issues and pull requests",
            description="Issues and pull requests you authored in repositories owned by others.",
            warning="Large searches can be slow.",
            record_kind="entity",
        ),
    ),
    analysis="github",
    check=checks.github,
)
