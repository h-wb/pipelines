"""Run one connection: register the operation, load with dlt, then build models.

Every execution path calls `run_connection`, so local runs, CLI requests, Prefect
schedules, and external (n8n) triggers all leave the same operation record.
"""

import json
import logging
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from src.core.config import ResolvedConnection, ResolvedWorkspace
from src.core.operations import (
    ConnectionBusy,
    MetadataStore,
    Operation,
    OperationError,
    Origin,
)
from src.integrations.registry import ModelBinding

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
MAX_ERROR_LENGTH = 500


@dataclass(frozen=True)
class RunOutcome:
    operation_id: str
    attempt: int
    ingestion: str
    transformation: str
    load_ids: tuple[str, ...]
    # False when the load finished but its outcome could not be written.
    recorded: bool = True


class IngestionFailed(RuntimeError):
    """The load failed; its redacted cause is on the attempt record."""


class AlreadyImported(OperationError):
    """The artifact was loaded before with the same parser and options."""

    def __init__(self, operation_id: str):
        super().__init__(f"Already imported by operation {operation_id}")
        self.operation_id = operation_id


def code_revision() -> str:
    """The deployed commit, from the environment or the checkout."""
    if revision := os.environ.get("PIPELINES_CODE_REVISION"):
        return revision[:64]
    try:
        head = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{head}+dirty" if dirty else head


def redact(error: BaseException, secrets: list[str]) -> tuple[str, str]:
    message = str(error)
    for secret in secrets:
        message = message.replace(secret, "***")
    kind = type(error)
    return f"{kind.__module__}.{kind.__qualname__}"[:200], message[:MAX_ERROR_LENGTH]


def model_selector(connection: ResolvedConnection) -> tuple[str | None, str]:
    """The dbt selector for this connection, or why models do not apply to it."""
    models: ModelBinding | None = connection.integration.models
    if models is None:
        return None, "integration has no models"
    if connection.binding.dataset_name != models.dataset_name:
        return None, f"models read {models.dataset_name}, not this connection"
    return f"source:{models.dbt_source}+", f"source:{models.dbt_source}+"


def _adopt_operation(
    store: MetadataStore,
    workspace: ResolvedWorkspace,
    connection: ResolvedConnection,
    origin: Origin,
    operation_id: str | None,
    backend_run_id: str | None,
) -> Operation:
    if operation_id is not None:
        operation = store.get_operation(operation_id)
        if (operation.workspace_id, operation.connection_id) != (
            workspace.id,
            connection.config.id,
        ):
            raise OperationError("Operation belongs to a different connection")
        if backend_run_id is not None:
            store.bind_backend_run(operation.id, backend_run_id)
        return operation
    if backend_run_id is not None:
        existing = store.find_by_backend_run(backend_run_id)
        if existing is not None:  # a retry of the same backend run
            if existing.connection_id != connection.config.id:
                raise OperationError("Backend run belongs to a different connection")
            return existing
    return store.create_operation(
        workspace_id=workspace.id,
        connection_id=connection.config.id,
        integration=connection.integration.name,
        kind="sync",
        origin=origin,
        pipeline_name=connection.binding.pipeline_name,
        dataset_name=connection.binding.dataset_name,
        config_revision=workspace.revision,
        code_revision=code_revision(),
        backend_run_id=backend_run_id,
    )


def run_connection(
    workspace: ResolvedWorkspace,
    connection_id: str,
    *,
    store: MetadataStore,
    destination: Any,
    resolve_credentials: Callable[[ResolvedConnection], dict[str, str]],
    origin: Origin,
    operation_id: str | None = None,
    backend_run_id: str | None = None,
    transform: Callable[[str], None] | None = None,
    pipelines_dir: str | None = None,
) -> RunOutcome:
    """Execute one sync attempt and record each stage.

    Registration must succeed before anything is loaded. After a load commits, a
    metadata failure is logged and reported as `recorded=False`, never as a failed
    load. A dbt failure leaves ingestion succeeded and the analysis stale.
    """
    connection = _connection(workspace, connection_id)
    operation = _adopt_operation(
        store, workspace, connection, origin, operation_id, backend_run_id
    )

    from src.integrations.sources import create_source

    return _run_locked(
        store,
        operation.id,
        connection,
        lambda credentials: create_source(connection, credentials),
        destination,
        resolve_credentials,
        transform,
        pipelines_dir,
    )


def import_artifact(
    workspace: ResolvedWorkspace,
    connection_id: str,
    artifact_id: str,
    *,
    store: MetadataStore,
    destination: Any,
    resolve_credentials: Callable[[ResolvedConnection], dict[str, str]],
    origin: Origin,
    replay: bool = False,
    transform: Callable[[str], None] | None = None,
    pipelines_dir: str | None = None,
) -> RunOutcome:
    """Parse one registered artifact and load its rows, bypassing file cursors.

    An artifact already imported with the same parser version and options is not
    loaded again unless `replay` is set; replays are merge-safe because records
    keep their source keys. A failed attempt never marks the artifact imported.
    """
    from src.core.artifacts import stored_copy_is_intact
    from src.integrations.formats import PARSER_VERSIONS
    from src.integrations.sources import create_artifact_source

    connection = _connection(workspace, connection_id)
    artifact = store.get_artifact(workspace.id, artifact_id)
    if artifact["connection_id"] != connection.config.id:
        raise OperationError("Artifact belongs to a different connection")
    parser_version = PARSER_VERSIONS[artifact["input_format"]]
    options = {"role": artifact["role"], **artifact_options(connection)}
    if not replay:
        for previous in store.artifact_imports(artifact_id):
            if (
                previous["ingestion"] == "succeeded"
                and previous["parser_version"] == parser_version
                and previous["options"] == options
            ):
                raise AlreadyImported(previous["operation_id"])
    if not stored_copy_is_intact(artifact):
        raise OperationError(
            "Stored artifact is missing or does not match its checksum"
        )
    operation = store.create_operation(
        workspace_id=workspace.id,
        connection_id=connection.config.id,
        integration=connection.integration.name,
        kind="replay" if replay else "import",
        origin=origin,
        pipeline_name=connection.binding.pipeline_name,
        dataset_name=connection.binding.dataset_name,
        config_revision=workspace.revision,
        code_revision=code_revision(),
        backend_run_id=None,
        artifact_id=artifact_id,
        parser_version=parser_version,
        options=json.dumps(options, sort_keys=True),
    )
    return _run_locked(
        store,
        operation.id,
        connection,
        lambda credentials: create_artifact_source(
            connection, artifact["storage_path"], options
        ),
        destination,
        resolve_credentials,
        transform,
        pipelines_dir,
    )


def artifact_options(connection: ResolvedConnection) -> dict[str, Any]:
    """Connection settings that change how an artifact is interpreted."""
    return {"cutoff": getattr(connection.settings, "cutoff", None)}


def _connection(workspace: ResolvedWorkspace, connection_id: str) -> ResolvedConnection:
    for connection in workspace.connections:
        if connection.config.id == connection_id:
            return connection
    raise OperationError("Unknown connection")


def _run_locked(
    store: MetadataStore,
    operation_id: str,
    connection: ResolvedConnection,
    make_source: Callable[[dict[str, str]], Any],
    destination: Any,
    resolve_credentials: Callable[[ResolvedConnection], dict[str, str]],
    transform: Callable[[str], None] | None,
    pipelines_dir: str | None,
) -> RunOutcome:
    try:
        with store.connection_lock(connection.binding.pipeline_name):
            attempt, number = store.start_attempt(operation_id, "running")
            return _execute(
                store,
                operation_id,
                attempt,
                number,
                connection,
                make_source,
                destination,
                resolve_credentials,
                transform,
                pipelines_dir,
            )
    except ConnectionBusy as error:
        attempt, _ = store.start_attempt(operation_id, "blocked")
        store.update_attempt(
            attempt, error_type="ConnectionBusy", error_message=str(error)
        )
        raise


def _execute(
    store: MetadataStore,
    operation_id: str,
    attempt: int,
    number: int,
    connection: ResolvedConnection,
    make_source: Callable[[dict[str, str]], Any],
    destination: Any,
    resolve_credentials: Callable[[ResolvedConnection], dict[str, str]],
    transform: Callable[[str], None] | None,
    pipelines_dir: str | None,
) -> RunOutcome:
    import dlt

    secrets: list[str] = []
    try:
        credentials = resolve_credentials(connection)
        secrets = list(credentials.values())
        # dlt's default working directory (~/.dlt/pipelines) unless one is given
        options: dict[str, Any] = (
            {"pipelines_dir": pipelines_dir} if pipelines_dir else {}
        )
        pipeline = dlt.pipeline(
            pipeline_name=connection.binding.pipeline_name,
            dataset_name=connection.binding.dataset_name,
            destination=destination,
            **options,
        )
        info = pipeline.run(make_source(credentials))
    except Exception as error:  # noqa: BLE001 - any failure is recorded, then raised
        error_type, message = redact(error, secrets)
        store.update_attempt(
            attempt,
            ingestion="failed",
            transformation="not_run",
            error_type=error_type,
            error_message=message,
        )
        raise IngestionFailed(f"{error_type}: {message}") from None

    load_ids = tuple(info.loads_ids)
    recorded = True
    try:
        store.update_attempt(attempt, ingestion="succeeded", load_ids=list(load_ids))
    except SQLAlchemyError:
        log.exception("Load %s succeeded but could not be recorded", load_ids)
        recorded = False

    selector, detail = model_selector(connection)
    if selector is None:
        transformation = "skipped"
    elif transform is None:
        transformation, detail = "skipped", "no transform configured"
    else:
        try:
            transform(selector)
            transformation = "succeeded"
        except Exception as error:  # noqa: BLE001 - dbt must not fail the load
            transformation = "failed"
            detail = ": ".join(redact(error, secrets))[:200]
            log.warning("Models after %s failed: %s", connection.config.id, detail)
    try:
        store.update_attempt(
            attempt, transformation=transformation, transformation_detail=detail
        )
    except SQLAlchemyError:
        log.exception("Transformation outcome could not be recorded")
        recorded = False
    return RunOutcome(
        operation_id, number, "succeeded", transformation, load_ids, recorded
    )
