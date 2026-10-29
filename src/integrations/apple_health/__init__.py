"""Apple Health through Health Auto Export JSON files."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any, Literal

from pydantic import Field, field_validator

from src.core.models import StrictModel
from src.integrations.apple_health.formats import validate_export
from src.integrations.contracts import (
    FileFormat,
    IntegrationDefinition,
    ResourceDefinition,
)


class AppleHealthSettings(StrictModel):
    cutoff: str = Field(
        title="Cutoff date",
        description="YYYY-MM-DD. Historical files contribute rows before it, "
        "ongoing files rows from it on",
    )

    @field_validator("cutoff")
    @classmethod
    def calendar_date(cls, value: str) -> str:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("expected YYYY-MM-DD")
        return value


class HealthImportOptions(StrictModel):
    role: Literal["ongoing", "historical"] = Field(
        default="ongoing",
        title="Dates to import",
        description="Historical files contribute rows before the cutoff; ongoing "
        "files contribute rows from it onwards.",
    )


@contextmanager
def open_export(settings: Any, path: str, options: dict[str, Any]) -> Iterator[Any]:
    from src.integrations.apple_health.source import apple_health_artifact

    yield apple_health_artifact(
        path=path, cutoff=options["cutoff"], historical=options["role"] == "historical"
    )


DEFINITION = IntegrationDefinition(
    name="apple_health",
    title="Apple Health (Health Auto Export)",
    settings_model=AppleHealthSettings,
    credentials=(),
    resources=(
        ResourceDefinition("metrics", ("metric_name", "date", "source"), None),
        ResourceDefinition("workouts", ("id",), None),
        ResourceDefinition("state_of_mind", ("id",), None),
        ResourceDefinition("medications", ("displayText", "start"), None),
    ),
    formats=(
        FileFormat(
            id="health_auto_export_json",
            version=1,
            label="Health Auto Export JSON",
            accept=".json,application/json",
            content_type="application/json",
            sender_instructions="In Health Auto Export, create a REST API automation "
            "and choose JSON as the export format.",
            help="Automatic exports use ongoing dates.",
            validate=validate_export,
            open=open_export,
            options=HealthImportOptions,
            interpretation=lambda settings: {"cutoff": settings.cutoff},
            default_pattern="*.json",
            sender="Health Auto Export",
            received_name="automatic-export.json",
        ),
    ),
    analysis="health",
)
