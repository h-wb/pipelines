"""Read and validate desired configuration without fetching secrets or running jobs."""

import fcntl
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote_plus

from pydantic import (
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from src.core.models import DatabaseIdentifier, Identifier, SecretKey, StrictModel
from src.integrations.contracts import IntegrationDefinition
from src.integrations.registry import INTEGRATIONS


def metadata_url() -> str | None:
    """The metadata store's URL from the environment, or None if unset.

    `MIROIR_METADATA_URL` wins when set. Otherwise the URL is composed from
    `MIROIR_METADATA__{HOST,USERNAME,PASSWORD}` (required) and
    `MIROIR_METADATA__{PORT,DATABASE}` (default 5432 and the username), so a
    cluster can mount credentials it already manages — e.g. the CNPG
    application secret — as environment variables without copying the password
    into a secret store.
    """
    if url := os.environ.get("MIROIR_METADATA_URL"):
        return url
    host = os.environ.get("MIROIR_METADATA__HOST")
    username = os.environ.get("MIROIR_METADATA__USERNAME")
    password = os.environ.get("MIROIR_METADATA__PASSWORD")
    if not (host and username and password):
        return None
    port = os.environ.get("MIROIR_METADATA__PORT") or "5432"
    database = os.environ.get("MIROIR_METADATA__DATABASE") or username
    # psycopg2 (the driver dlt already ships): SQLAlchemy's bare `postgresql://`
    # resolves to psycopg 3, which is not installed.
    return (
        f"postgresql+psycopg2://{quote_plus(username)}:{quote_plus(password)}"
        f"@{host}:{port}/{database}"
    )


class ConfigurationError(ValueError):
    """A safe diagnostic that must not include configuration input values."""


class ConfigurationConflict(ConfigurationError):
    """The file changed since the editor read it."""


class SecretReference(StrictModel):
    """Where a credential's value lives. Never the value itself.

    `miroir` secrets are stored encrypted by the miroir and identified by
    their connection and credential name, so they take no key. External
    providers (`env`, `fnox`) name the key they resolve.
    """

    provider: Literal["miroir", "env", "fnox"]
    key: SecretKey | None = None

    @model_validator(mode="after")
    def key_matches_provider(self) -> "SecretReference":
        if (self.provider == "miroir") != (self.key is None):
            raise ValueError("miroir secrets take no key; env and fnox need one")
        return self


class Binding(StrictModel):
    pipeline_name: DatabaseIdentifier
    dataset_name: DatabaseIdentifier


WebUrl = Annotated[str, StringConstraints(pattern=r"^https?://[^\s]+$", max_length=500)]


class Links(StrictModel):
    """Where to send people for execution logs and analyses. Optional."""

    prefect_ui: WebUrl | None = None
    metabase: WebUrl | None = None


class Schedule(StrictModel):
    """When the orchestrator should sync the connection. Not an execution input."""

    cron: str = Field(max_length=100)
    timezone: str = "UTC"

    @field_validator("cron")
    @classmethod
    def valid_cron(cls, value: str) -> str:
        from croniter import croniter  # type: ignore[import-untyped]

        if not croniter.is_valid(value):
            raise ValueError("expected a cron expression")
        return value

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("expected an IANA time zone") from None
        return value


class AnalysisSettings(StrictModel):
    """Interpretation for analysis, independent of source credentials/delivery."""

    timezone: str = "UTC"
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        return Schedule.known_timezone(value)


class Folder(StrictModel):
    """A watched folder (decision 021): new matching files become artifacts.

    Delivery, not a source setting: the same files could be uploaded or sent.
    """

    path: str = Field(
        min_length=1,
        max_length=1000,
        title="Server folder",
        description="An absolute path readable by the server and the jobs running "
        "this connection.",
    )
    # Glob relative to `path`; the format's default when omitted.
    pattern: str | None = Field(default=None, min_length=1, max_length=200)
    # One of the integration's formats; its first when omitted.
    format: Identifier | None = None
    # Import options for every file from this folder, validated by the format.
    options: dict[str, Any] = Field(default_factory=dict)
    # Stored copies kept for folder files; older imported ones are removed.
    # None keeps every copy.
    keep_last: int | None = Field(default=5, ge=1, le=1000)

    @field_validator("path")
    @classmethod
    def absolute(cls, value: str) -> str:
        if not value.startswith("/") or "\x00" in value:
            raise ValueError("expected an absolute path")
        return value

    @field_validator("pattern")
    @classmethod
    def relative_glob(cls, value: str | None) -> str | None:
        if value is not None and (
            value.startswith("/") or ".." in Path(value).parts or "\x00" in value
        ):
            raise ValueError("expected a pattern inside the folder")
        return value


class ConnectionConfig(StrictModel):
    id: Identifier
    integration: Identifier
    settings: dict[str, Any]
    credentials: dict[str, SecretReference] = Field(default_factory=dict)
    binding: Binding | None = None
    resources: list[Identifier] | None = None
    folder: Folder | None = None
    schedule: Schedule | None = None
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)
    # Metabase dashboard name -> ID, shown as links next to the connection.
    dashboards: dict[
        Annotated[str, StringConstraints(min_length=1, max_length=100)],
        Annotated[int, Field(gt=0)],
    ] = Field(default_factory=dict)


class Deployment(StrictModel):
    """Where and how Prefect executes this miroir's connections (decision 016).

    The configuration is the single source of desired execution state, so any
    writer — the interface or the CLI — plans and applies the same deployments.
    Like schedules, it is not part of a connection's execution revision.
    """

    work_pool: str = Field(min_length=1, max_length=100)
    # The image flow runs execute; a digest-pinned tag (ghcr.io/h-wb/pipelines:sha-…).
    image: str | None = None
    # Job variables for the work pool (e.g. the namespace); None falls back to
    # the environment default, an empty object replaces it with nothing.
    job_variables: dict[str, Any] | None = None


class MiroirConfig(StrictModel):
    schema_version: Literal[1]
    id: Identifier
    # Empty is valid: a new installation starts with no connections.
    connections: list[ConnectionConfig] = Field(default_factory=list)
    links: Links = Field(default_factory=Links)
    # Absent: execution settings come from the writer's environment (the
    # MIROIR_* defaults); present: they are authoritative here instead.
    deployment: Deployment | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("expected an integer schema version")
        return value


@dataclass(frozen=True)
class ResolvedConnection:
    config: ConnectionConfig
    integration: IntegrationDefinition
    settings: StrictModel
    binding: Binding
    # Hash of this connection's source execution configuration (with miroir ID).
    # Analysis has its own plan receipt; timezone edits don't invalidate ingestion.
    revision: str = ""
    miroir_id: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.config.id,
            "integration": self.integration.name,
            "binding": self.binding.model_dump(),
            "explicit_binding": self.config.binding is not None,
            "credential_names": list(self.integration.credential_names),
            "resources": list(self.config.resources)
            if self.config.resources is not None
            else [resource.name for resource in self.integration.resources],
            "resource_definitions": [
                {"name": r.name, "record_kind": r.record_kind, "history": r.history}
                for r in self.integration.resources
            ],
            "capabilities": list(self.integration.capabilities),
            "fetch": self.integration.fetch is not None,
            "folder": self.config.folder.model_dump() if self.config.folder else None,
        }

    @property
    def can_sync(self) -> bool:
        """A sync fetches from the API and/or scans the watched folder."""
        return self.integration.fetch is not None or self.config.folder is not None


@dataclass(frozen=True)
class ResolvedMiroir:
    id: str
    revision: str
    connections: tuple[ResolvedConnection, ...]
    links: Links = field(default_factory=Links)
    # Declared execution settings (decision 016); None when the writer's
    # environment supplies them instead.
    deployment: Deployment | None = None


def _validation_error(error: ValidationError, prefix: str) -> ConfigurationError:
    # Pydantic messages can include supplied values. Emit only known field paths
    # and machine error codes. Unknown keys are not echoed either.
    issues = []
    for issue in error.errors(include_input=False, include_context=False):
        location = list(issue["loc"])
        if issue["type"] == "extra_forbidden" and location:
            location[-1] = "<unknown field>"
        # Credential keys are user-controlled and may themselves contain a secret.
        if "credentials" in location:
            i = location.index("credentials") + 1
            if i < len(location):
                location[i] = "<credential>"
        issues.append(f"{prefix}.{'.'.join(map(str, location))}: {issue['type']}")
    return ConfigurationError("; ".join(issues))


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError("Duplicate JSON object key")
        result[key] = value
    return result


def parse_miroir(text: str) -> ResolvedMiroir:
    try:
        raw = json.loads(text, object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError) as error:
        raise ConfigurationError("Invalid JSON configuration") from error
    if isinstance(raw, dict):
        for connection in raw.get("connections") or []:
            _upgrade_legacy_folder(connection)
    try:
        config = MiroirConfig.model_validate(raw)
    except ValidationError as error:
        raise _validation_error(error, "miroir") from error

    connections = []
    ids: set[str] = set()
    pipelines: set[str] = set()
    datasets: set[str] = set()
    for index, connection in enumerate(config.connections):
        prefix = f"connections.{index}"
        definition = INTEGRATIONS.get(connection.integration)
        if definition is None:
            raise ConfigurationError(f"{prefix}.integration: unknown integration")
        try:
            settings = definition.settings_model.model_validate(connection.settings)
        except ValidationError as error:
            raise _validation_error(error, f"{prefix}.settings") from error
        if set(connection.credentials) != set(definition.credential_names):
            raise ConfigurationError(
                f"{prefix}.credentials: incorrect credential names"
            )
        if connection.folder is not None:
            connection = connection.model_copy(
                update={
                    "folder": _resolve_folder(connection.folder, definition, prefix)
                }
            )
        if connection.resources is not None:
            if definition.fetch is None:
                raise ConfigurationError(
                    f"{prefix}.resources: only fetched resources can be selected"
                )
            available = {r.name for r in definition.resources}
            if (
                not connection.resources
                or len(set(connection.resources)) != len(connection.resources)
                or set(connection.resources) - available
            ):
                raise ConfigurationError(
                    f"{prefix}.resources: select distinct declared resources"
                )
        from src.analyses.catalog import ANALYSES

        analysis = ANALYSES.get(definition.analysis or "")
        try:
            (analysis.parameters if analysis else StrictModel).model_validate(
                connection.analysis.parameters
            )
        except ValidationError as error:
            raise _validation_error(error, f"{prefix}.analysis.parameters") from error
        # Length-prefix the miroir ID so (a_b, c) and (a, b_c) cannot alias.
        name = f"ws_{len(config.id)}_{config.id}_{connection.id}"
        binding = connection.binding or Binding(pipeline_name=name, dataset_name=name)
        for value, seen, label in (
            (connection.id, ids, "id"),
            (binding.pipeline_name, pipelines, "pipeline_name"),
            (binding.dataset_name, datasets, "dataset_name"),
        ):
            if value in seen:
                raise ConfigurationError(
                    f"{prefix}.{label}: duplicate binding or identity"
                )
            seen.add(value)
        connections.append(
            ResolvedConnection(connection, definition, settings, binding)
        )

    # Normalize defaults, typed settings, and bindings before computing the revision.
    # It identifies desired configuration only; nothing is persisted or deployed here.
    entries = {
        c.config.id: {
            "id": c.config.id,
            "integration": c.integration.name,
            "settings": c.settings.model_dump(),
            "credentials": {k: v.model_dump() for k, v in c.config.credentials.items()},
            "binding": c.binding.model_dump(),
            "analysis": c.config.analysis.model_dump(),
            **(
                {"resources": c.config.resources}
                if c.config.resources is not None
                else {}
            ),
            # Retention does not change what a sync loads.
            **(
                {"folder": c.config.folder.model_dump(exclude={"keep_last"})}
                if c.config.folder is not None
                else {}
            ),
        }
        for c in connections
    }
    connections = [
        replace(
            c,
            revision=_hash(
                {
                    "miroir": config.id,
                    **{
                        k: v for k, v in entries[c.config.id].items() if k != "analysis"
                    },
                }
            ),
            miroir_id=config.id,
        )
        for c in connections
    ]
    normalized = {
        "schema_version": config.schema_version,
        "id": config.id,
        "connections": [entries[key] for key in sorted(entries)],
    }
    return ResolvedMiroir(
        config.id,
        _hash(normalized),
        tuple(connections),
        links=config.links,
        deployment=config.deployment,
    )


def _upgrade_legacy_folder(connection: Any) -> None:
    """Read a pre-021 `settings.data_dir` as the connection's watched folder."""
    if not isinstance(connection, dict):
        return
    settings = connection.get("settings")
    definition = INTEGRATIONS.get(str(connection.get("integration")))
    if not (isinstance(settings, dict) and "data_dir" in settings and definition):
        return
    if not definition.formats:
        return
    path = settings.pop("data_dir")
    if path is not None and "folder" not in connection:
        connection["folder"] = {"path": path}


def _resolve_folder(
    folder: Folder, definition: IntegrationDefinition, prefix: str
) -> Folder:
    """Fill the format, its default pattern, and validated import options."""
    if not definition.formats:
        raise ConfigurationError(f"{prefix}.folder: this integration reads no files")
    try:
        file_format = definition.format(folder.format)
    except KeyError:
        raise ConfigurationError(
            f"{prefix}.folder.format: not a format of this integration"
        ) from None
    try:
        options = file_format.options.model_validate(folder.options)
    except ValidationError as error:
        raise _validation_error(error, f"{prefix}.folder.options") from error
    return folder.model_copy(
        update={
            "format": file_format.id,
            "pattern": folder.pattern or file_format.default_pattern,
            "options": options.model_dump(mode="json"),
        }
    )


def _hash(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def load_miroir(path: Path) -> ResolvedMiroir:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ConfigurationError("Cannot read UTF-8 configuration file") from error
    return parse_miroir(text)


def save_miroir(path: Path, text: str, expected_revision: str) -> ResolvedMiroir:
    """Replace the configuration file if it is still at `expected_revision`.

    The revision check and the write happen under one lock, and the new file is
    validated, flushed, and renamed into place, so a concurrent CLI or UI edit
    cannot be silently overwritten and readers never see a partial file.
    """
    miroir = parse_miroir(text)
    with open(path.with_name(f".{path.name}.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if file_revision(path) != expected_revision:
            raise ConfigurationConflict(
                "Configuration changed since it was loaded; reload and reapply"
            )
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as target:
                target.write(text)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
        except BaseException:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise
    return miroir


def file_revision(path: Path) -> str:
    """The exact file contents an editor saw; a missing file is "missing".

    Stricter than the configuration revision on purpose: any byte change by
    someone else, even formatting or link edits, is a conflict.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing"
