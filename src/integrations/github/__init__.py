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
    import requests

    from src.integrations.github.source import github_source

    with requests.Session() as session:
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
    resources=tuple(
        ResourceDefinition(name, key, cursor, kind)
        for name, key, cursor, kind in (
            ("repos", ("id",), None, "entity"),
            ("stars", ("repo__id",), None, "event"),
            ("events", ("id",), None, "event"),
            ("contributions_daily", ("date",), "last_date", "snapshot"),
            *(
                (
                    name,
                    ("html_url",),
                    "last_run" if name != "releases" else None,
                    "event" if "commits" in name else "entity",
                )
                for name in (
                    "commits",
                    "pull_requests",
                    "issues",
                    "issue_comments",
                    "releases",
                    "external_commits",
                    "external_issues",
                )
            ),
        )
    ),
    analysis="github",
    check=checks.github,
)
