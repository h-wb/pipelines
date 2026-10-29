"""Open a connector's declared inputs with scoped credentials and explicit cleanup."""

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
def open_fetch(
    connection: ResolvedConnection, credentials: dict[str, str]
) -> Iterator[Any]:
    """The connector's API fetch, limited to the connection's selected resources."""
    _credentials(connection, credentials)
    fetch = connection.integration.fetch
    if fetch is None:
        raise OperationError("This integration has nothing to fetch")
    with fetch.open(connection.settings, credentials) as source:
        resources = connection.config.resources
        yield source.with_resources(*resources) if resources else source


@contextmanager
def open_import(
    connection: ResolvedConnection,
    input_format: str,
    path: str,
    options: dict[str, Any],
) -> Iterator[Any]:
    """One stored file, parsed by its format with its recorded import options."""
    try:
        definition = connection.integration.format(input_format)
    except KeyError:
        raise OperationError("This integration cannot read that file format") from None
    with definition.open(connection.settings, path, options) as source:
        yield source
