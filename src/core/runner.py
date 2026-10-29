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

from src.core.analysis import AnalysisPlan, analysis_plan
from src.core.config import ResolvedConnection, ResolvedMiroir
from src.core.operations import (
    ConnectionBusy,
    MetadataStore,
    Operation,
    OperationError,
    Origin,
)

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
    # Longest first, so a secret containing another is fully masked.
    for secret in sorted(secrets, key=len, reverse=True):
        message = message.replace(secret, "***")
    kind = type(error)
    return f"{kind.__module__}.{kind.__qualname__}"[:200], message[:MAX_ERROR_LENGTH]


def failure_kind(error: BaseException) -> str | None:
    """Classify a failure from its exception chain (dlt wraps source errors)."""
    from src.core.secrets import SecretKeyError, SecretResolutionError

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (SecretResolutionError, SecretKeyError)):
            return "credentials_missing"
        response = getattr(current, "response", None)
        if getattr(response, "status_code", None) in (401, 403):
            return "credentials_rejected"
        current = (
            current.__cause__
            or current.__context__
            or getattr(current, "exception", None)
        )
    return None


def request_models(
    miroir: ResolvedMiroir,
    connection_id: str,
    *,
    store: MetadataStore,
    origin: Origin,
) -> Operation:
    connection = _connection(miroir, connection_id)
    plan = analysis_plan(connection)
    if plan is None:
        raise OperationError("This integration has no analysis models")
    if (
        store.connection_status(miroir.id, connection_id)["last_ingestion_success"]
        is None
    ):
        raise OperationError("Import or sync data before rebuilding analysis")
    return _new_operation(
        store,
        miroir,
        connection,
        origin,
        kind="models",
        options=json.dumps({"analysis_receipt": plan.receipt}),
    )


def _new_operation(
    store: MetadataStore,
    miroir: ResolvedMiroir,
    connection: ResolvedConnection,
    origin: Origin,
    kind: str = "sync",
    **values: Any,
) -> Operation:
    values.setdefault("backend_run_id", None)
    return store.create_operation(
        miroir_id=miroir.id,
        connection_id=connection.config.id,
        integration=connection.integration.name,
        kind=kind,
        origin=origin,
        pipeline_name=connection.binding.pipeline_name,
        dataset_name=connection.binding.dataset_name,
        config_revision=connection.revision,
        code_revision=code_revision(),
        **values,
    )


def request_sync(
    miroir: ResolvedMiroir,
    connection_id: str,
    *,
    store: MetadataStore,
    origin: Origin,
) -> Operation:
    """Record a sync request. It shows as requested until an executor runs it."""
    connection = _connection(miroir, connection_id)
    if not connection.integration.can_sync(connection.settings):
        raise OperationError("Upload an export or configure a server folder to scan")
    if not {"incremental_sync", "directory_import"} & set(
        connection.integration.capabilities
    ):
        raise OperationError("Integration has nothing to sync")
    return _new_operation(store, miroir, connection, origin)


def request_import(
    miroir: ResolvedMiroir,
    connection_id: str,
    artifact_id: str,
    *,
    store: MetadataStore,
    origin: Origin,
    replay: bool = False,
) -> Operation:
    """Record an import of one artifact, or raise AlreadyImported.

    An artifact already imported with the same parser version and options is not
    imported again unless `replay` is set; replays are merge-safe because records
    keep their source keys. A failed attempt never marks the artifact imported.
    """
    from src.integrations.formats import PARSER_VERSIONS

    connection = _connection(miroir, connection_id)
    artifact = store.get_artifact(miroir.id, artifact_id)
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
    return _new_operation(
        store,
        miroir,
        connection,
        origin,
        kind="replay" if replay else "import",
        artifact_id=artifact_id,
        parser_version=parser_version,
        options=json.dumps(options, sort_keys=True),
    )


def execute_operation(
    miroir: ResolvedMiroir,
    operation_id: str,
    *,
    store: MetadataStore,
    destination: Any,
    resolve_credentials: Callable[[ResolvedConnection], dict[str, str]],
    backend_run_id: str | None = None,
    transform: Callable[[AnalysisPlan], None] | None = None,
    pipelines_dir: str | None = None,
    lock_wait: float = 0,
) -> RunOutcome:
    """Run one recorded operation as a new attempt and record each stage.

    `lock_wait` lets queued requests wait for a running execution of the same
    connection instead of being blocked (several exports arriving together).

    Registration must already have succeeded. After a load commits, a metadata
    failure is logged and reported as `recorded=False`, never as a failed load. A
    dbt failure leaves ingestion succeeded and the analysis stale.

    The operation runs only against the connection configuration it was requested
    with: an edit saved in between blocks it instead of silently changing it.
    """
    from src.core.artifacts import stored_copy_is_intact
    from src.integrations.formats import PARSER_VERSIONS
    from src.integrations.sources import create_artifact_source, create_source

    operation = store.get_operation(operation_id)
    if operation.miroir_id != miroir.id:
        raise OperationError("Operation belongs to a different miroir")
    connection = _connection(miroir, operation.connection_id)
    if backend_run_id is not None:
        store.bind_backend_run(operation.id, backend_run_id)

    reason = None
    make_source: Callable[[dict[str, str]], Any] | None = None
    if operation.config_revision != connection.revision:
        reason = "Connection configuration changed since this operation was requested"
    elif operation.kind == "sync":
        make_source = lambda credentials: create_source(connection, credentials)
    elif operation.kind == "models":
        plan = analysis_plan(connection)
        if plan is None:
            reason = "This integration has no analysis models"
        elif (
            json.loads(operation.options or "{}").get("analysis_receipt")
            != plan.receipt
        ):
            reason = (
                "Analysis settings or models changed since this rebuild was requested"
            )
    else:
        artifact = store.get_artifact(miroir.id, str(operation.artifact_id))
        options = json.loads(operation.options or "{}")
        if operation.parser_version != PARSER_VERSIONS[artifact["input_format"]]:
            reason = "Parser version changed since this import was requested"
        elif not stored_copy_is_intact(artifact):
            reason = "Stored artifact is missing or does not match its checksum"
        path = artifact["storage_path"]
        make_source = lambda credentials: create_artifact_source(
            connection, path, options
        )
    if reason is not None:
        attempt, _ = store.start_attempt(operation.id, "blocked")
        store.update_attempt(attempt, error_type="Blocked", error_message=reason)
        raise OperationError(reason)
    return _run_locked(
        store,
        operation.id,
        connection,
        make_source,
        destination,
        resolve_credentials,
        transform,
        pipelines_dir,
        lock_wait,
    )


def run_connection(
    miroir: ResolvedMiroir,
    connection_id: str,
    *,
    store: MetadataStore,
    origin: Origin,
    operation_id: str | None = None,
    backend_run_id: str | None = None,
    **execution: Any,
) -> RunOutcome:
    """Sync one connection now, adopting a requested operation or a backend retry.

    Without an operation ID, a backend run that already has an operation (a
    Prefect retry) adds an attempt to it; anything else registers a new one.
    """
    connection = _connection(miroir, connection_id)
    if operation_id is None and backend_run_id is not None:
        existing = store.find_by_backend_run(backend_run_id)
        if existing is not None:
            operation_id = existing.id
    if operation_id is None:
        operation_id = _new_operation(
            store, miroir, connection, origin, backend_run_id=backend_run_id
        ).id
    elif store.get_operation(operation_id).connection_id != connection.config.id:
        raise OperationError("Operation belongs to a different connection")
    return execute_operation(
        miroir,
        operation_id,
        store=store,
        backend_run_id=backend_run_id,
        **execution,
    )


def import_artifact(
    miroir: ResolvedMiroir,
    connection_id: str,
    artifact_id: str,
    *,
    store: MetadataStore,
    origin: Origin,
    replay: bool = False,
    **execution: Any,
) -> RunOutcome:
    """Request and immediately execute an artifact import."""
    operation = request_import(
        miroir, connection_id, artifact_id, store=store, origin=origin, replay=replay
    )
    return execute_operation(miroir, operation.id, store=store, **execution)


def artifact_options(connection: ResolvedConnection) -> dict[str, Any]:
    """Connection settings that change how an artifact is interpreted."""
    return {"cutoff": getattr(connection.settings, "cutoff", None)}


def _connection(miroir: ResolvedMiroir, connection_id: str) -> ResolvedConnection:
    for connection in miroir.connections:
        if connection.config.id == connection_id:
            return connection
    raise OperationError("Unknown connection")


def _run_locked(
    store: MetadataStore,
    operation_id: str,
    connection: ResolvedConnection,
    make_source: Callable[[dict[str, str]], Any] | None,
    destination: Any,
    resolve_credentials: Callable[[ResolvedConnection], dict[str, str]],
    transform: Callable[[AnalysisPlan], None] | None,
    pipelines_dir: str | None,
    lock_wait: float = 0,
) -> RunOutcome:
    try:
        with store.connection_lock(connection.binding.pipeline_name, lock_wait):
            attempt, number = store.start_attempt(
                operation_id, "not_run" if make_source is None else "running"
            )
            if make_source is None:
                transformation, recorded = _transform(
                    store, attempt, connection, transform, []
                )
                return RunOutcome(
                    operation_id, number, "not_run", transformation, (), recorded
                )
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
    transform: Callable[[AnalysisPlan], None] | None,
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
            failure_kind=failure_kind(error),
        )
        raise IngestionFailed(f"{error_type}: {message}") from None

    load_ids = tuple(info.loads_ids)
    recorded = True
    try:
        store.update_attempt(attempt, ingestion="succeeded", load_ids=list(load_ids))
    except SQLAlchemyError:
        log.exception("Load %s succeeded but could not be recorded", load_ids)
        recorded = False

    transformation, models_recorded = _transform(
        store, attempt, connection, transform, secrets
    )
    return RunOutcome(
        operation_id,
        number,
        "succeeded",
        transformation,
        load_ids,
        recorded and models_recorded,
    )


def _transform(
    store: MetadataStore,
    attempt: int,
    connection: ResolvedConnection,
    transform: Callable[[AnalysisPlan], None] | None,
    secrets: list[str],
) -> tuple[str, bool]:
    plan = analysis_plan(connection)
    detail = plan.receipt if plan else "integration has no models"
    recorded = True
    if plan is None:
        transformation = "skipped"
    elif transform is None:
        transformation, detail = "skipped", "no transform configured"
    else:
        try:
            store.update_attempt(attempt, transformation="running")
        except SQLAlchemyError:
            log.exception("Transformation start could not be recorded")
            recorded = False
        try:
            transform(plan)
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
    return transformation, recorded
