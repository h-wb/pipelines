"""Metadata and configuration for the first bundled integrations.

The registry deliberately does not import sources, dlt, or Prefect. Capability
metadata describes the underlying integration, not implemented CLI commands.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any

from pydantic import Field, field_validator

from src.core.models import StrictModel
from src.integrations import checks
from src.integrations.delivery import DeliveryMethod, FileDelivery


class ListenBrainzSettings(StrictModel):
    username: str = Field(
        min_length=1,
        title="Username",
        description="Your ListenBrainz account name",
    )
    start_date: str = Field(
        title="Start from",
        description="Earliest listen to fetch, with a UTC offset "
        "(e.g. 2020-01-01T00:00:00Z)",
    )

    @field_validator("username")
    @classmethod
    def valid_username(cls, value: str) -> str:
        if value.strip() != value or "/" in value or not value.strip():
            raise ValueError("expected an account name")
        return value

    @field_validator("start_date")
    @classmethod
    def aware_timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("expected a timestamp with a UTC offset")
        return parsed.isoformat()


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


@dataclass(frozen=True)
class ResourceDefinition:
    name: str
    primary_key: tuple[str, ...]
    cursor: str | None


@dataclass(frozen=True)
class ModelBinding:
    """The dbt source that reads this integration, and the one schema it reads.

    dbt source schemas are fixed in `dbt/models/staging/_sources.yml`, so models
    only see a connection bound to exactly that dataset.
    """

    dbt_source: str
    dataset_name: str


@dataclass(frozen=True)
class CredentialField:
    """A secret the integration needs. Stored and shown write-only."""

    name: str
    label: str
    help: str


@dataclass(frozen=True)
class IntegrationDefinition:
    name: str
    title: str
    settings_model: type[StrictModel]
    credentials: tuple[CredentialField, ...]
    input_modes: tuple[str, ...]
    capabilities: tuple[str, ...]
    resources: tuple[ResourceDefinition, ...]
    delivery_methods: tuple[DeliveryMethod, ...] = ()
    file_delivery: FileDelivery | None = None
    input_format: str | None = None
    models: ModelBinding | None = None
    # Confirms settings and credentials without loading data; see checks.py.
    check: Callable[[Any, dict[str, str]], str] | None = None

    @property
    def credential_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.credentials)

    def can_sync(self, settings: Any) -> bool:
        return any(method.can_sync(settings) for method in self.delivery_methods)


INTEGRATIONS = MappingProxyType(
    {
        "listenbrainz": IntegrationDefinition(
            name="listenbrainz",
            title="ListenBrainz",
            settings_model=ListenBrainzSettings,
            credentials=(
                CredentialField(
                    "access_token",
                    "User token",
                    "ListenBrainz → Settings → User token "
                    "(https://listenbrainz.org/settings/)",
                ),
            ),
            check=checks.listenbrainz,
            input_modes=("api",),
            capabilities=("incremental_sync",),
            delivery_methods=(
                DeliveryMethod(
                    "api",
                    "Connect your account",
                    "Miroir fetches your data using your account's access token.",
                ),
            ),
            resources=(
                ResourceDefinition(
                    "listens", ("listened_at", "recording_msid"), "listened_at"
                ),
            ),
            models=ModelBinding("listenbrainz", "listenbrainz_data"),
        ),
        "apple_health": IntegrationDefinition(
            name="apple_health",
            title="Apple Health (Health Auto Export)",
            settings_model=AppleHealthSettings,
            credentials=(),
            check=checks.apple_health,
            input_modes=("file",),
            capabilities=("directory_import", "artifact_import"),
            input_format="health_auto_export_json",
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
            models=ModelBinding("apple_health", "apple_health_data"),
        ),
    }
)
