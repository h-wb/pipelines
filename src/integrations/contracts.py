"""Connector contracts. No source, database, or orchestrator imports at discovery."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.models import StrictModel
from src.integrations.delivery import DeliveryMethod, FileDelivery


@dataclass(frozen=True)
class ResourceDefinition:
    name: str
    primary_key: tuple[str, ...]
    cursor: str | None
    record_kind: str = "event"
    history: str = "Previously observed rows are retained; upstream absence does not delete history."


@dataclass(frozen=True)
class CredentialField:
    """A secret the integration needs. Stored and shown write-only."""

    name: str
    label: str
    help: str


@dataclass(frozen=True)
class FileInput:
    id: str
    parser_version: int
    validate: Callable[[Path], None]
    options: Callable[[Any], dict[str, Any]]
    open: Callable[
        [Any, dict[str, str], str, dict[str, Any]], AbstractContextManager[Any]
    ]


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
    file_input: FileInput | None = None
    analysis: str | None = None
    open_sync: Callable[[Any, dict[str, str]], AbstractContextManager[Any]] | None = (
        None
    )
    # Confirms settings and credentials without loading data; see checks.py.
    check: Callable[[Any, dict[str, str]], str] | None = None

    @property
    def credential_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.credentials)

    def can_sync(self, settings: Any) -> bool:
        return any(method.can_sync(settings) for method in self.delivery_methods)

    @property
    def input_format(self) -> str | None:
        return self.file_input.id if self.file_input else None
