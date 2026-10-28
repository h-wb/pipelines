"""Lazy adapters to existing dlt sources with explicit per-connection inputs.

Callers supply resolved credentials. This module does not resolve secret references,
set environment variables, choose a destination, or start a pipeline.
"""

from typing import TYPE_CHECKING

from src.core.config import ResolvedConnection
from src.integrations.registry import AppleHealthSettings, ListenBrainzSettings

if TYPE_CHECKING:
    from dlt.extract import DltSource


def create_source(
    connection: ResolvedConnection, credentials: dict[str, str]
) -> "DltSource":
    if set(credentials) != set(connection.integration.credential_names):
        raise ValueError("Incorrect resolved credential names")
    if any(not isinstance(value, str) or not value for value in credentials.values()):
        raise ValueError("Resolved credentials must be nonempty strings")
    settings = connection.settings
    if isinstance(settings, ListenBrainzSettings):
        from src.sources.listenbrainz import listenbrainz_source

        return listenbrainz_source(
            username=settings.username,
            access_token=credentials["access_token"],
            start_date=settings.start_date,
        )
    if isinstance(settings, AppleHealthSettings):
        from src.sources.apple_health import apple_health

        return apple_health(data_dir=settings.data_dir, cutoff=settings.cutoff)
    raise ValueError("No source adapter for the validated integration")
