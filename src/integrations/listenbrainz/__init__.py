"""listenbrainz connection definition and lazy execution hooks."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from src.core.models import StrictModel
from src.integrations import checks
from src.integrations.contracts import (
    CredentialField,
    IntegrationDefinition,
    ResourceDefinition,
)
from src.integrations.delivery import DeliveryMethod


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


@contextmanager
def open_sync(settings: Any, credentials: dict[str, str]) -> Iterator[Any]:
    import requests

    from src.integrations.listenbrainz.source import listenbrainz_source

    with requests.Session() as session:
        yield listenbrainz_source(
            username=settings.username,
            access_token=credentials["access_token"],
            start_date=settings.start_date,
            session=session,
        )


DEFINITION = IntegrationDefinition(
    name="listenbrainz",
    open_sync=open_sync,
    title="ListenBrainz",
    settings_model=ListenBrainzSettings,
    credentials=(
        CredentialField(
            "access_token",
            "User token",
            "ListenBrainz → Settings → User token (https://listenbrainz.org/settings/)",
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
        ResourceDefinition("listens", ("listened_at", "recording_msid"), "listened_at"),
    ),
    analysis="music",
)
