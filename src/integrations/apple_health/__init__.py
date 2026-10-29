"""apple_health connection definition and lazy execution hooks."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

from pydantic import Field, field_validator

from src.core.models import StrictModel
from src.integrations import checks
from src.integrations.contracts import (
    FileInput,
    IntegrationDefinition,
    ResourceDefinition,
)
from src.integrations.delivery import DeliveryMethod, FileDelivery


class AppleHealthSettings(StrictModel):
    data_dir: str | None = Field(
        default=None,
        min_length=1,
        title="Server folder",
        description="A folder readable by the server and the jobs running this connection.",
    )
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


from src.integrations.validation import _health_auto_export_json


@contextmanager
def open_sync(settings: Any, credentials: dict[str, str]) -> Iterator[Any]:
    from src.core.operations import OperationError
    from src.integrations.apple_health.source import apple_health

    if settings.data_dir is None:
        raise OperationError("Upload an export or configure a server folder to scan")
    yield apple_health(data_dir=settings.data_dir, cutoff=settings.cutoff)


@contextmanager
def open_artifact(
    settings: Any, credentials: dict[str, str], path: str, options: dict[str, Any]
) -> Iterator[Any]:
    from src.integrations.apple_health.source import apple_health_artifact

    yield apple_health_artifact(
        path=path, cutoff=options["cutoff"], historical=options["role"] == "historical"
    )


DEFINITION = IntegrationDefinition(
    name="apple_health",
    open_sync=open_sync,
    title="Apple Health (Health Auto Export)",
    settings_model=AppleHealthSettings,
    credentials=(),
    check=checks.apple_health,
    input_modes=("file",),
    capabilities=("directory_import", "artifact_import"),
    delivery_methods=(
        DeliveryMethod(
            "upload",
            "Upload files",
            "Choose exports from your device whenever you want to import them.",
        ),
        DeliveryMethod(
            "webhook",
            "Health Auto Export",
            "Send exports automatically from your phone to a private receiving URL.",
        ),
        DeliveryMethod(
            "folder",
            "Scan a server folder",
            "Read exports from a folder on your server.",
            ("data_dir",),
            advanced=True,
        ),
    ),
    file_input=FileInput(
        "health_auto_export_json",
        1,
        _health_auto_export_json,
        lambda settings: {"cutoff": settings.cutoff},
        open_artifact,
    ),
    file_delivery=FileDelivery(
        label="Health Auto Export JSON",
        accept=".json,application/json",
        content_type="application/json",
        sender_instructions="In Health Auto Export, create a REST API automation and choose JSON as the export format.",
        role_help="Historical files contribute rows before the cutoff; ongoing files contribute rows from it onwards.",
    ),
    resources=(
        ResourceDefinition("metrics", ("metric_name", "date", "source"), None),
        ResourceDefinition("workouts", ("id",), None),
        ResourceDefinition("state_of_mind", ("id",), None),
        ResourceDefinition("medications", ("displayText", "start"), None),
    ),
    analysis="health",
)
