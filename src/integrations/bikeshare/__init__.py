"""Bike Share Toronto mobile-API connection."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import Field

from src.core.models import StrictModel
from src.integrations import checks
from src.integrations.contracts import (
    CredentialField,
    IntegrationDefinition,
    ResourceDefinition,
)
from src.integrations.delivery import DeliveryMethod


class BikeShareSettings(StrictModel):
    verify_tls: bool = Field(
        default=True,
        title="Verify server certificate",
        description="Keep enabled when possible. The mobile API has historically required an explicit certificate-verification exception.",
    )


@contextmanager
def open_sync(settings: Any, credentials: dict[str, str]) -> Iterator[Any]:
    import requests

    from src.integrations.bikeshare.source import bikeshare

    with requests.Session() as session:
        session.verify = settings.verify_tls
        yield bikeshare(
            member_id=credentials["member_id"],
            authorization_token=credentials["authorization_token"],
            session=session,
        )


DEFINITION = IntegrationDefinition(
    name="bikeshare",
    title="Bike Share Toronto",
    settings_model=BikeShareSettings,
    credentials=(
        CredentialField(
            "member_id", "Member ID", "Your Bike Share mobile-app member identifier."
        ),
        CredentialField(
            "authorization_token",
            "Authorization token",
            "The Authorization value used by the mobile app. Replace it here when it expires.",
        ),
    ),
    input_modes=("api",),
    capabilities=("incremental_sync",),
    delivery_methods=(
        DeliveryMethod(
            "api",
            "Connect your account",
            "Fetch your closed rentals using your mobile-app credentials.",
        ),
    ),
    resources=(ResourceDefinition("trips", ("rentalId",), "sd"),),
    analysis="biking",
    check=checks.bikeshare,
    open_sync=open_sync,
)
