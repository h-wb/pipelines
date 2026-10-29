"""Open declared source hooks with scoped credentials and explicit cleanup."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from src.core.config import ResolvedConnection
from src.core.operations import OperationError


def _credentials(connection: ResolvedConnection, credentials: dict[str, str]) -> None:
    if set(credentials) != set(connection.integration.credential_names):
        raise ValueError("Incorrect resolved credential names")
    if any(not isinstance(v, str) or not v for v in credentials.values()):
        raise ValueError("Resolved credentials must be nonempty strings")


@contextmanager
def open_source(
    connection: ResolvedConnection, credentials: dict[str, str]
) -> Iterator[Any]:
    _credentials(connection, credentials)
    opener = connection.integration.open_sync
    if opener is None:
        raise OperationError("This integration has no sync method")
    with opener(connection.settings, credentials) as source:
        yield (
            source.with_resources(*connection.config.resources)
            if connection.config.resources
            else source
        )


@contextmanager
def open_artifact_source(
    connection: ResolvedConnection,
    credentials: dict[str, str],
    path: str,
    options: dict[str, Any],
) -> Iterator[Any]:
    _credentials(connection, credentials)
    definition = connection.integration.file_input
    if definition is None:
        raise OperationError("This integration has no artifact parser")
    with definition.open(connection.settings, credentials, path, options) as source:
        yield (
            source.with_resources(*connection.config.resources)
            if connection.config.resources
            else source
        )
