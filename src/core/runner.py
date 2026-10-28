"""Run one connection: register the operation, load with dlt, then build models.

Every execution path calls `run_connection`, so local runs, CLI requests, Prefect
schedules, and external (n8n) triggers all leave the same operation record.
"""

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
    connection = next(
        (c for c in workspace.connections if c.config.id == connection_id), None
    )
    if connection is None:
        raise OperationError("Unknown connection")
    operation = _adopt_operation(
        store, workspace, connection, origin, operation_id, backend_run_id
    )

    try:
        with store.connection_lock(connection.binding.pipeline_name):
            attempt, number = store.start_attempt(operation.id, "running")
            return _execute(
                store,
                operation.id,
                attempt,
                number,
                connection,
                destination,
                resolve_credentials,
                transform,
                pipelines_dir,
            )
    except ConnectionBusy as error:
        attempt, _ = store.start_attempt(operation.id, "blocked")
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
    destination: Any,
    resolve_credentials: Callable[[ResolvedConnection], dict[str, str]],
    transform: Callable[[str], None] | None,
    pipelines_dir: str | None,
) -> RunOutcome:
    import dlt

    from src.integrations.sources import create_source

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
        info = pipeline.run(create_source(connection, credentials))
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
