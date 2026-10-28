"""Application services shared by the CLI and the HTTP API.

Everything a person can do goes through here: read status, request a sync,
submit and import files, and edit configuration. Transports only translate
requests and responses. Execution is handed to an executor, so the same request
can run in-process, in a subprocess, or (later) as a Prefect flow run.
"""

import logging
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from src.core.config import (
    ResolvedConnection,
    ResolvedWorkspace,
    file_revision,
    load_workspace,
    save_workspace,
)
from src.core.operations import MetadataStore, OperationError, Role

REPO = Path(__file__).resolve().parents[2]
log = logging.getLogger(__name__)


class Executor(Protocol):
    def submit(self, operation_id: str) -> None: ...


@dataclass
class InlineExecutor:
    """Runs the operation before returning. Failures are on the attempt record."""

    execute: Callable[[str], Any]

    def submit(self, operation_id: str) -> None:
        try:
            self.execute(operation_id)
        except Exception:  # noqa: BLE001 - the runner recorded it; status shows it
            log.info("Operation %s failed; see its attempt record", operation_id)


@dataclass
class SubprocessExecutor:
    """Runs each operation as `pipelines-workspace execute` in its own process.

    A crash or kill only affects that operation, as on a worker. Finished
    children are reaped on the next submission.
    """

    arguments: list[str]
    children: list[subprocess.Popen] = field(default_factory=list)

    def submit(self, operation_id: str) -> None:
        self.children = [child for child in self.children if child.poll() is None]
        self.children.append(
            subprocess.Popen(
                [sys.executable, "-m", "src.cli", *self.arguments, operation_id],
                cwd=REPO,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        )


@dataclass
class WorkspaceService:
    config_path: Path
    store: MetadataStore
    artifact_root: Path
    executor: Executor

    def workspace(self) -> ResolvedWorkspace:
        return load_workspace(self.config_path)

    def _connection(
        self, workspace: ResolvedWorkspace, connection_id: str
    ) -> ResolvedConnection:
        for connection in workspace.connections:
            if connection.config.id == connection_id:
                return connection
        raise OperationError("Unknown connection")

    def _links(
        self, workspace: ResolvedWorkspace, connection: ResolvedConnection
    ) -> dict:
        metabase = workspace.links.metabase
        return {
            "dashboards": [
                {"name": name, "url": f"{metabase.rstrip('/')}/dashboard/{id_}"}
                for name, id_ in connection.config.dashboards.items()
            ]
            if metabase
            else [],
        }

    def run_link(
        self, workspace: ResolvedWorkspace, backend_run_id: str | None
    ) -> str | None:
        ui = workspace.links.prefect_ui
        if ui and backend_run_id:
            return f"{ui.rstrip('/')}/runs/flow-run/{backend_run_id}"
        return None

    def overview(self) -> dict[str, Any]:
        workspace = self.workspace()
        return {
            "workspace": workspace.id,
            "revision": workspace.revision,
            "connections": [self._summary(workspace, c) for c in workspace.connections],
        }

    def _summary(
        self, workspace: ResolvedWorkspace, connection: ResolvedConnection
    ) -> dict:
        from src.core.runner import model_selector

        status = self.store.connection_status(workspace.id, connection.config.id)
        selector, reason = model_selector(connection)
        if selector is None and status["analysis"] != "no_data":
            # Loaded data no model reads is not "stale": nothing will ever build it.
            status["analysis"] = "not_modelled"
            status["analysis_detail"] = reason
        last = status["last_attempt"]
        if last is not None:
            last["run_url"] = self.run_link(workspace, last.get("backend_run_id"))
        applied = self.store.last_executed_revision(workspace.id, connection.config.id)
        return {
            **connection.summary(),
            "status": status,
            # Saved configuration vs the configuration its last execution used.
            "configuration": "never_run"
            if applied is None
            else "applied"
            if applied == connection.revision
            else "saved_not_applied",
            "can_sync": bool(
                {"incremental_sync", "directory_import"}
                & set(connection.integration.capabilities)
            ),
            "sync_label": "Sync now"
            if "incremental_sync" in connection.integration.capabilities
            else "Scan folder",
            "accepts_files": "artifact_import" in connection.integration.capabilities,
            **self._links(workspace, connection),
        }

    def connection(self, connection_id: str) -> dict[str, Any]:
        workspace = self.workspace()
        connection = self._connection(workspace, connection_id)
        operations = self.store.recent_operations(workspace.id, connection_id)
        artifacts = self.artifacts(connection_id, workspace)
        names = {artifact["id"]: artifact["original_name"] for artifact in artifacts}
        for operation in operations:
            operation["run_url"] = self.run_link(workspace, operation["backend_run_id"])
            operation["artifact_name"] = names.get(operation["artifact_id"])
        return {
            **self._summary(workspace, connection),
            "operations": operations,
            "artifacts": artifacts,
        }

    def artifacts(
        self, connection_id: str, workspace: ResolvedWorkspace | None = None
    ) -> list:
        workspace = workspace or self.workspace()
        self._connection(workspace, connection_id)
        return [
            {**artifact, "imports": self.store.artifact_imports(artifact["id"])}
            for artifact in reversed(
                self.store.list_artifacts(workspace.id, connection_id)
            )
        ]

    def request_sync(self, connection_id: str, origin: Any = "request") -> str:
        from src.core.runner import request_sync

        operation = request_sync(
            self.workspace(), connection_id, store=self.store, origin=origin
        )
        self.executor.submit(operation.id)
        return operation.id

    def submit_file(
        self,
        connection_id: str,
        path: Path,
        role: Role,
        *,
        import_now: bool = False,
        original_name: str | None = None,
    ) -> dict[str, Any]:
        from src.core.artifacts import submit_artifact
        from src.core.runner import AlreadyImported

        workspace = self.workspace()
        artifact, created = submit_artifact(
            workspace,
            self._connection(workspace, connection_id),
            path,
            role,
            store=self.store,
            root=self.artifact_root,
            original_name=original_name,
        )
        result: dict[str, Any] = {"artifact": artifact, "new": created}
        if import_now:
            try:
                result["operation_id"] = self.request_import(
                    connection_id, artifact["id"]
                )
            except AlreadyImported as error:
                result["already_imported"] = error.operation_id
        return result

    def request_import(
        self,
        connection_id: str,
        artifact_id: str,
        replay: bool = False,
        origin: Any = "request",
    ) -> str:
        from src.core.runner import request_import

        operation = request_import(
            self.workspace(),
            connection_id,
            artifact_id,
            store=self.store,
            origin=origin,
            replay=replay,
        )
        self.executor.submit(operation.id)
        return operation.id

    def configuration(self) -> dict[str, Any]:
        from src.core.config import ConfigurationError

        try:
            text = self.config_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            text = ""
        try:
            load_workspace(self.config_path)
            error = None
        except ConfigurationError as problem:
            error = str(problem)
        return {
            "text": text,
            "file_revision": file_revision(self.config_path),
            "error": error,
        }

    def save_configuration(self, text: str, file_revision: str) -> ResolvedWorkspace:
        return save_workspace(self.config_path, text, file_revision)
