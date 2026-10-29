"""Source-local analysis metadata; SQL dependencies remain owned by dbt."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import Field, field_validator

from src.core.models import StrictModel


class AnalysisParameters(StrictModel):
    pass


class GitHubParameters(StrictModel):
    author_emails: list[str] = Field(
        default_factory=list,
        title="Your commit author emails",
        description="One email per line. Helps identify commits whose email is not linked to your GitHub account.",
    )

    @field_validator("author_emails")
    @classmethod
    def emails(cls, values: list[str]) -> list[str]:
        if any(
            "@" not in v
            or not v.strip()
            or len(v) > 254
            or any(c in v for c in "\n\r\x00")
            for v in values
        ):
            raise ValueError("expected email addresses")
        return sorted({v.strip().lower() for v in values})


def _no_variables(source_settings: Any, parameters: Any) -> dict[str, Any]:
    return {}


def _github_variables(
    source_settings: Any, parameters: GitHubParameters
) -> dict[str, Any]:
    # Account identity is an input to the GitHub analysis, never a global env override.
    return {
        "github_login": source_settings.username,
        "github_author_emails": parameters.author_emails,
    }


@dataclass(frozen=True)
class AnalysisDefinition:
    id: str
    dbt_source: str
    title: str
    selector: str
    group: str
    relations: tuple[str, ...]
    parameters: type[StrictModel] = AnalysisParameters
    version: int = 1
    # dbt variables from the source settings and validated analysis parameters.
    bind: Callable[[Any, Any], dict[str, Any]] = _no_variables

    def variables(
        self, source_settings: Any, parameters: dict[str, Any]
    ) -> dict[str, Any]:
        return self.bind(source_settings, self.parameters.model_validate(parameters))


ANALYSES = {
    "music": AnalysisDefinition(
        "music",
        "listenbrainz",
        "Music analysis",
        "+path:models/music",
        "music",
        (
            "daily",
            "listens",
            "artists",
            "sessions",
            "monthly_top",
            "streaks",
            "on_repeat",
        ),
    ),
    "health": AnalysisDefinition(
        "health",
        "apple_health",
        "Health analysis",
        "+path:models/health",
        "health",
        ("daily_summary", "workout_summary"),
    ),
    "github": AnalysisDefinition(
        "github",
        "github",
        "GitHub analysis",
        "+path:models/github",
        "github",
        (
            "commits",
            "pull_requests",
            "issues",
            "issue_comments",
            "releases",
            "contributions",
        ),
        GitHubParameters,
        bind=_github_variables,
    ),
    "biking": AnalysisDefinition(
        "biking",
        "bikeshare",
        "Bike share analysis",
        "+path:models/biking",
        "biking",
        ("rides", "stations", "routes"),
    ),
    "browser": AnalysisDefinition(
        "browser",
        "browser_history",
        "Browser analysis",
        "+path:models/browser",
        "browser",
        ("visits", "domains_daily"),
    ),
}
