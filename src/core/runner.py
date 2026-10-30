"""Run one connection: register the operation, load with dlt, then build models.

Every execution path calls `run_connection`, so local runs, CLI requests, Prefect
schedules, and external (n8n) triggers all leave the same operation record.

A sync fetches from the connector's API and/or collects its watched folder: new
files become artifacts and are imported as child operations, then the analysis
is built once (decision 021).
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

from src.core.analysis import (
    AnalysisPlan,
    analyses_linking,
    analysis_plan,
    find_analysis,
)
from src.core.config import ResolvedAnalysis, ResolvedConnection, ResolvedMiroir
from src.core.operations import (
    ArtifactOrigin,
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
    analysis_id: str,
    *,
    store: MetadataStore,
    origin: Origin,
) -> Operation:
    """Record a rebuild of one analysis from already loaded data.

    It runs through the first linked input's connection (its deployment and
    lock); nothing is fetched or loaded.
    """
    analysis = find_analysis(miroir, analysis_id)
    linked = list(analysis.inputs.values())
    status = store.analysis_status(
        miroir.id, analysis.id, [c.config.id for c in linked]
    )
    if status["state"] == "no_data":
        raise OperationError("Import or sync data before rebuilding analysis")
    return _new_operation(
        store,
        miroir,
        linked[0],
        origin,
        kind="models",
        analysis_id=analysis.id,
        options=json.dumps({"analysis_receipt": analysis_plan(analysis).receipt}),
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
    if not connection.can_sync:
        raise OperationError(
            "Nothing to sync: send or upload files, or configure a watched folder"
        )
    return _new_operation(store, miroir, connection, origin)


def request_import(
    miroir: ResolvedMiroir,
    connection_id: str,
    artifact_id: str,
    *,
    store: MetadataStore,
    origin: Origin,
    replay: bool = False,
    parent_operation_id: str | None = None,
) -> Operation:
    """Record an import of one artifact, or raise AlreadyImported.

    An artifact already imported with the same parser version and options is not
    imported again unless `replay` is set; replays are merge-safe because records
    keep their source keys. A failed attempt never marks the artifact imported.
    """
    connection = _connection(miroir, connection_id)
    artifact = store.get_artifact(miroir.id, artifact_id)
    if artifact["connection_id"] != connection.config.id:
        raise OperationError("Artifact belongs to a different connection")
    parser_version = _format(connection, artifact).version
    options = import_options(connection, artifact)
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
        parent_operation_id=parent_operation_id,
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
    artifact_root: Path | None = None,
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
    from src.integrations.sources import open_fetch, open_import

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
        if connection.config.folder is not None and artifact_root is None:
            reason = "No artifact storage configured for collecting the folder"
        elif connection.integration.fetch is not None:
            make_source = lambda credentials: open_fetch(connection, credentials)
    elif operation.kind == "models":
        analysis = next(
            (a for a in miroir.analyses if a.id == operation.analysis_id), None
        )
        if analysis is None:
            reason = "This analysis no longer exists"
        elif (
            json.loads(operation.options or "{}").get("analysis_receipt")
            != analysis_plan(analysis).receipt
        ):
            reason = (
                "Analysis settings or models changed since this rebuild was requested"
            )
    else:
        artifact = store.get_artifact(miroir.id, str(operation.artifact_id))
        options = json.loads(operation.options or "{}")
        if operation.parser_version != _format(connection, artifact).version:
            reason = "Parser version changed since this import was requested"
        elif artifact["removed_at"] is not None:
            reason = (
                "Retention removed the stored copy; send the file again to import it"
            )
        elif not stored_copy_is_intact(artifact):
            reason = "Stored artifact is missing or does not match its checksum"
        path, input_format = artifact["storage_path"], artifact["input_format"]
        make_source = lambda credentials: open_import(
            connection, input_format, path, options
        )
    if reason is not None:
        attempt, _ = store.start_attempt(operation.id, "blocked")
        store.update_attempt(attempt, error_type="Blocked", error_message=reason)
        raise OperationError(reason)
    if operation.kind == "sync" and connection.config.folder is not None:
        assert artifact_root is not None  # checked above
        return _sync_with_folder(
            miroir,
            operation,
            connection,
            make_source,
            store=store,
            destination=destination,
            resolve_credentials=resolve_credentials,
            transform=transform,
            pipelines_dir=pipelines_dir,
            lock_wait=lock_wait,
            artifact_root=artifact_root,
        )
    return _run_locked(
        store,
        operation,
        miroir,
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


def import_options(
    connection: ResolvedConnection, artifact: dict[str, Any]
) -> dict[str, Any]:
    """The file's own import options plus connection settings that change how
    every file of its format is read (Health's cutoff). Recorded per import."""
    file_format = _format(connection, artifact)
    return {**artifact["options"], **file_format.interpretation(connection.settings)}


def _format(connection: ResolvedConnection, artifact: dict[str, Any]) -> Any:
    try:
        return connection.integration.format(artifact["input_format"])
    except KeyError:
        raise OperationError(
            "This integration no longer reads that file format"
        ) from None


def collect_folder(
    miroir: ResolvedMiroir,
    connection: ResolvedConnection,
    *,
    store: MetadataStore,
    artifact_root: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Register the watched folder's matching files as artifacts.

    Returns (artifacts, rejected file names). Unchanged files map to their
    existing artifact; invalid files are skipped and reported, never stored.
    """
    from src.core.artifacts import submit_artifact
    from src.integrations.validation import InvalidArtifact

    folder = connection.config.folder
    assert folder is not None and folder.pattern is not None
    root = Path(folder.path)
    if not root.is_dir():
        raise OperationError("The watched folder does not exist or is not a folder")
    collected, rejected = [], []
    for path in sorted(p for p in root.glob(folder.pattern) if p.is_file()):
        origin: ArtifactOrigin = "folder"
        try:
            artifact, _ = submit_artifact(
                miroir,
                connection,
                path,
                folder.options,
                store=store,
                root=artifact_root,
                origin=origin,
                input_format=folder.format,
            )
        except (InvalidArtifact, OperationError):
            rejected.append(path.name)
            continue
        collected.append(artifact)
    return collected, rejected


def prune_folder_artifacts(
    miroir: ResolvedMiroir, connection: ResolvedConnection, *, store: MetadataStore
) -> list[str]:
    """Remove stored copies of older folder files beyond `keep_last`.

    Only files that were imported successfully are removed; their receipts and
    history stay. Returns the removed artifact IDs.
    """
    folder = connection.config.folder
    if folder is None or folder.keep_last is None:
        return []
    candidates = sorted(
        (
            a
            for a in store.list_artifacts(miroir.id, connection.config.id)
            if a["origin"] == "folder" and a["removed_at"] is None
        ),
        key=lambda a: a["last_received_at"],
        reverse=True,
    )[folder.keep_last :]
    removed = []
    for artifact in candidates:
        if not any(
            i["ingestion"] == "succeeded"
            for i in store.artifact_imports(artifact["id"])
        ):
            continue
        Path(artifact["storage_path"]).unlink(missing_ok=True)
        store.mark_artifact_removed(miroir.id, artifact["id"])
        removed.append(artifact["id"])
    return removed


def _sync_with_folder(
    miroir: ResolvedMiroir,
    operation: Operation,
    connection: ResolvedConnection,
    make_source: Callable[[dict[str, str]], Any] | None,
    *,
    store: MetadataStore,
    destination: Any,
    resolve_credentials: Callable[[ResolvedConnection], dict[str, str]],
    transform: Callable[[AnalysisPlan], None] | None,
    pipelines_dir: str | None,
    lock_wait: float,
    artifact_root: Path,
) -> RunOutcome:
    """Fetch (if the connector can), import the folder's new files, build once.

    Each new file is its own import operation, linked to this sync. Imports
    take the connection lock themselves, so the fetch releases it first. A scan
    that loads nothing records ingestion `not_run` and builds nothing.
    """
    execution: dict[str, Any] = {
        "store": store,
        "destination": destination,
        "resolve_credentials": resolve_credentials,
        "pipelines_dir": pipelines_dir,
        "lock_wait": lock_wait,
    }
    load_ids: list[str] = []
    try:
        with store.connection_lock(connection.binding.pipeline_name, lock_wait):
            attempt, number = store.start_attempt(operation.id, "running")
            if make_source is not None:
                # A failed fetch is recorded on this attempt and ends the sync.
                fetched = _execute(
                    store,
                    operation,
                    attempt,
                    number,
                    miroir,
                    connection,
                    make_source,
                    destination,
                    resolve_credentials,
                    None,
                    pipelines_dir,
                )
                load_ids += fetched.load_ids
    except ConnectionBusy as error:
        blocked, _ = store.start_attempt(operation.id, "blocked")
        store.update_attempt(
            blocked, error_type="ConnectionBusy", error_message=str(error)
        )
        raise
    failures: list[str] = []
    try:
        collected, rejected = collect_folder(
            miroir, connection, store=store, artifact_root=artifact_root
        )
    except (OSError, OperationError) as error:
        store.update_attempt(
            attempt,
            ingestion="failed",
            transformation="not_run",
            error_type=type(error).__name__,
            error_message=str(error)[:MAX_ERROR_LENGTH],
        )
        raise IngestionFailed(str(error)) from None
    failures += [f"{name}: not a valid file" for name in rejected]
    for artifact in collected:
        try:
            child = request_import(
                miroir,
                connection.config.id,
                artifact["id"],
                store=store,
                origin=operation.origin,  # type: ignore[arg-type]
                parent_operation_id=operation.id,
            )
        except AlreadyImported:
            continue
        try:
            outcome = execute_operation(miroir, child.id, **execution)
        except (IngestionFailed, OperationError) as error:
            failures.append(f"{artifact['original_name']}: {error}")
            continue
        load_ids += outcome.load_ids
    prune_folder_artifacts(miroir, connection, store=store)
    message = "; ".join(failures)[:MAX_ERROR_LENGTH]
    if failures:
        # Files that did load are still modelled below.
        store.update_attempt(
            attempt,
            ingestion="failed",
            error_type="FolderImportFailed",
            error_message=message,
            load_ids=load_ids,
            **({} if load_ids else {"transformation": "not_run"}),
        )
    elif not load_ids:
        store.update_attempt(
            attempt,
            ingestion="not_run",
            transformation="not_run",
            transformation_detail="No new data",
        )
        return RunOutcome(operation.id, number, "not_run", "not_run", (), True)
    else:
        store.update_attempt(attempt, ingestion="succeeded", load_ids=load_ids)
    transformation, recorded = (
        _transform(store, operation, attempt, miroir, connection, transform, [])
        if load_ids
        else ("not_run", True)
    )
    if failures:
        raise IngestionFailed(f"FolderImportFailed: {message}")
    return RunOutcome(
        operation.id,
        number,
        "succeeded",
        transformation,
        tuple(load_ids),
        recorded,
    )


def _connection(miroir: ResolvedMiroir, connection_id: str) -> ResolvedConnection:
    for connection in miroir.connections:
        if connection.config.id == connection_id:
            return connection
    raise OperationError("Unknown connection")


def _run_locked(
    store: MetadataStore,
    operation: Operation,
    miroir: ResolvedMiroir,
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
                operation.id, "not_run" if make_source is None else "running"
            )
            if make_source is None:
                transformation, recorded = _transform(
                    store, operation, attempt, miroir, connection, transform, []
                )
                return RunOutcome(
                    operation.id, number, "not_run", transformation, (), recorded
                )
            return _execute(
                store,
                operation,
                attempt,
                number,
                miroir,
                connection,
                make_source,
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
    operation: Operation,
    attempt: int,
    number: int,
    miroir: ResolvedMiroir,
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
        with make_source(credentials) as source:
            info = pipeline.run(source)
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
        store, operation, attempt, miroir, connection, transform, secrets
    )
    return RunOutcome(
        operation.id,
        number,
        "succeeded",
        transformation,
        load_ids,
        recorded and models_recorded,
    )


def _transform(
    store: MetadataStore,
    operation: Operation,
    attempt: int,
    miroir: ResolvedMiroir,
    connection: ResolvedConnection,
    transform: Callable[[AnalysisPlan], None] | None,
    secrets: list[str],
) -> tuple[str, bool]:
    """Build after a load: every analysis linking the connection, once each.

    A requested rebuild builds exactly its analysis on its own attempt. After a
    load, each linked analysis is a child `models` operation with its own
    attempt; the load attempt records a summary. A failed build never fails the
    load (decision 019).
    """
    if operation.kind == "models":
        analysis = find_analysis(miroir, str(operation.analysis_id))
        return _build(store, attempt, analysis, transform, secrets)
    analyses = analyses_linking(miroir, connection.config.id)
    detail = None
    if not analyses:
        transformation, detail = "skipped", "No analysis links this connection"
    elif transform is None:
        transformation, detail = "skipped", "no transform configured"
    if detail is not None:
        try:
            store.update_attempt(
                attempt, transformation=transformation, transformation_detail=detail
            )
        except SQLAlchemyError:
            log.exception("Transformation outcome could not be recorded")
            return transformation, False
        return transformation, True
    recorded = True
    outcomes = []
    try:
        store.update_attempt(attempt, transformation="running")
    except SQLAlchemyError:
        log.exception("Transformation start could not be recorded")
        recorded = False
    for analysis in analyses:
        try:
            child = _new_operation(
                store,
                miroir,
                connection,
                operation.origin,  # type: ignore[arg-type]
                kind="models",
                analysis_id=analysis.id,
                parent_operation_id=operation.id,
                options=json.dumps(
                    {"analysis_receipt": analysis_plan(analysis).receipt}
                ),
            )
            child_attempt, _ = store.start_attempt(child.id, "not_run")
        except SQLAlchemyError:
            log.exception("Build of %s could not be registered", analysis.id)
            outcomes.append((analysis.id, "failed"))
            recorded = False
            continue
        outcome, ok = _build(store, child_attempt, analysis, transform, secrets)
        outcomes.append((analysis.id, outcome))
        recorded = recorded and ok
    transformation = (
        "succeeded" if all(o == "succeeded" for _, o in outcomes) else "failed"
    )
    summary = ", ".join(f"{analysis_id} {outcome}" for analysis_id, outcome in outcomes)
    try:
        store.update_attempt(
            attempt,
            transformation=transformation,
            transformation_detail=f"Analyses: {summary}"[:200],
        )
    except SQLAlchemyError:
        log.exception("Transformation outcome could not be recorded")
        recorded = False
    return transformation, recorded


def _build(
    store: MetadataStore,
    attempt: int,
    analysis: ResolvedAnalysis,
    transform: Callable[[AnalysisPlan], None] | None,
    secrets: list[str],
) -> tuple[str, bool]:
    """Run one analysis build and record it on `attempt`."""
    plan = analysis_plan(analysis)
    detail = plan.receipt
    recorded = True
    if transform is None:
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
            log.warning("Analysis %s failed: %s", analysis.id, detail)
    try:
        store.update_attempt(
            attempt, transformation=transformation, transformation_detail=detail
        )
    except SQLAlchemyError:
        log.exception("Transformation outcome could not be recorded")
        recorded = False
    return transformation, recorded
