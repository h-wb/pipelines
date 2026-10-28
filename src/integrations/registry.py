"""Metadata and configuration for the first bundled integrations.

The registry deliberately does not import sources, dlt, or Prefect. Capability
metadata describes the underlying integration, not implemented CLI commands.
"""

from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType

from pydantic import Field, field_validator

from src.core.models import StrictModel


class ListenBrainzSettings(StrictModel):
    username: str = Field(min_length=1)
    start_date: str

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
    data_dir: str = Field(min_length=1)
    cutoff: str

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
class IntegrationDefinition:
    name: str
    settings_model: type[StrictModel]
    credential_names: tuple[str, ...]
    input_modes: tuple[str, ...]
    capabilities: tuple[str, ...]
    resources: tuple[ResourceDefinition, ...]
    input_format: str | None = None
    models: ModelBinding | None = None


INTEGRATIONS = MappingProxyType(
    {
        "listenbrainz": IntegrationDefinition(
            name="listenbrainz",
            settings_model=ListenBrainzSettings,
            credential_names=("access_token",),
            input_modes=("api",),
            capabilities=("incremental_sync",),
            resources=(
                ResourceDefinition(
                    "listens", ("listened_at", "recording_msid"), "listened_at"
                ),
            ),
            models=ModelBinding("listenbrainz", "listenbrainz_data"),
        ),
        "apple_health": IntegrationDefinition(
            name="apple_health",
            settings_model=AppleHealthSettings,
            credential_names=(),
            input_modes=("file",),
            capabilities=("directory_import",),
            input_format="health_auto_export_json",
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
