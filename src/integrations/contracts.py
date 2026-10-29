"""Connector contracts (decision 021). No source, database, or orchestrator imports.

A connector declares what it can read: a fetch and/or file formats. How files
arrive (upload, receiving URL, watched folder) belongs to core, not connectors.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.core.models import StrictModel
from src.integrations.delivery import DeliveryMethod


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


class NoOptions(StrictModel):
    """A format whose files need no per-import choices."""


def _no_interpretation(settings: Any) -> dict[str, Any]:
    return {}


@dataclass(frozen=True)
class Fetch:
    """Pull from the source's API with the connection's credentials."""

    label: str
    description: str
    # (settings, credentials) -> context manager yielding a dlt source
    open: Callable[[Any, dict[str, str]], AbstractContextManager[Any]]


@dataclass(frozen=True)
class FileFormat:
    """One kind of file the connector parses, however it was delivered."""

    id: str
    # Bump when parsing changes which rows or values a file produces.
    version: int
    label: str
    accept: str
    content_type: str
    sender_instructions: str
    help: str
    validate: Callable[[Path], None]
    # (settings, stored path, import options) -> context manager yielding a dlt source
    open: Callable[[Any, str, dict[str, Any]], AbstractContextManager[Any]]
    # Choices made per file (upload form, folder configuration).
    options: type[StrictModel] = NoOptions
    # Connection settings that change how every file is read; recorded per import.
    interpretation: Callable[[Any], dict[str, Any]] = _no_interpretation
    default_pattern: str = "*"
    # The app or automation that usually sends these files, when there is one.
    sender: str = ""
    # Name recorded for files arriving at the receiving URL.
    received_name: str = "received"


@dataclass(frozen=True)
class IntegrationDefinition:
    name: str
    title: str
    settings_model: type[StrictModel]
    credentials: tuple[CredentialField, ...]
    resources: tuple[ResourceDefinition, ...]
    fetch: Fetch | None = None
    formats: tuple[FileFormat, ...] = ()
    analysis: str | None = None
    # Confirms settings and credentials without loading data; see checks.py.
    check: Callable[[Any, dict[str, str]], str] | None = None

    @property
    def credential_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.credentials)

    @property
    def accepts_files(self) -> bool:
        return bool(self.formats)

    @property
    def capabilities(self) -> tuple[str, ...]:
        derived = []
        if self.fetch:
            derived += ["fetch", "resource_selection"]
        if self.formats:
            derived += ["files", "watched_folder"]
        return tuple(derived)

    def format(self, format_id: str | None = None) -> FileFormat:
        """The named format, or the connector's first one."""
        for definition in self.formats:
            if format_id in (None, definition.id):
                return definition
        raise KeyError(format_id)

    @property
    def delivery_methods(self) -> tuple[DeliveryMethod, ...]:
        """Setup choices, derived from the inputs; transports are core's."""
        methods = []
        if self.fetch:
            methods.append(
                DeliveryMethod("api", self.fetch.label, self.fetch.description)
            )
        if self.formats:
            label, sender = self.formats[0].label, self.formats[0].sender
            methods += [
                DeliveryMethod(
                    "upload",
                    "Upload files",
                    f"Choose {label} files from your device whenever you want.",
                ),
                DeliveryMethod(
                    "webhook",
                    sender or "Send automatically",
                    f"{sender or 'A device or automation'} sends files to a private "
                    "receiving URL.",
                ),
                DeliveryMethod(
                    "folder",
                    "Watch a server folder",
                    "Import new files from a folder readable by the server and "
                    "its jobs, on each sync.",
                    advanced=True,
                ),
            ]
        return tuple(methods)

    def describe(self) -> dict[str, Any]:
        """JSON-ready metadata for the CLI, API, and forms. No callables."""
        return {
            "name": self.name,
            "title": self.title,
            "settings_schema": self.settings_model.model_json_schema(),
            "credentials": [asdict(field) for field in self.credentials],
            "credential_names": list(self.credential_names),
            "resources": [asdict(resource) for resource in self.resources],
            "capabilities": list(self.capabilities),
            "fetch": {"label": self.fetch.label, "description": self.fetch.description}
            if self.fetch
            else None,
            "formats": [
                {
                    "id": f.id,
                    "version": f.version,
                    "label": f.label,
                    "accept": f.accept,
                    "content_type": f.content_type,
                    "sender_instructions": f.sender_instructions,
                    "help": f.help,
                    "default_pattern": f.default_pattern,
                    "options_schema": f.options.model_json_schema(),
                }
                for f in self.formats
            ],
            "delivery_methods": [asdict(m) for m in self.delivery_methods],
            "analysis": self.analysis,
        }
