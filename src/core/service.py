"""Application services shared by the CLI and the HTTP API.

Everything a person can do goes through here: read status, request a sync,
submit and import files, and edit configuration. Transports only translate
requests and responses. Execution is handed to an executor, so the same request
can run in-process, in a subprocess, or (later) as a Prefect flow run.
"""

import hashlib
import hmac
import logging
import secrets as random_secrets
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from src.core.config import (
    ResolvedConnection,
    ResolvedMiroir,
    file_revision,
    load_miroir,
    save_miroir,
)
from src.core.operations import ArtifactOrigin, MetadataStore, OperationError
from src.core.secrets import SecretResolutionError, SecretStore, make_resolver

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
    """Runs each operation as `pipelines-miroir execute` in its own process.

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
class MiroirService:
    config_path: Path
    store: MetadataStore
    artifact_root: Path
    executor: Executor
    # None when MIROIR_SECRET_KEY is not set: built-in secrets are unavailable,
    # external (env/fnox) references still work.
    secrets: SecretStore | None = None
    # The Prefect adapter when an orchestrator is configured (decision 013). It is
    # then also the executor; without it, requests run as local subprocesses.
    orchestrator: Any = None

    def resolver(self) -> Callable[[ResolvedConnection], dict[str, str]]:
        return make_resolver(self.secrets)

    def miroir(self) -> ResolvedMiroir:
        return load_miroir(self.config_path)

    def _connection(
        self, miroir: ResolvedMiroir, connection_id: str
    ) -> ResolvedConnection:
        for connection in miroir.connections:
            if connection.config.id == connection_id:
                return connection
        raise OperationError("Unknown connection")

    def _links(self, miroir: ResolvedMiroir, connection: ResolvedConnection) -> dict:
        metabase = miroir.links.metabase
        return {
            "dashboards": [
                {"name": name, "url": f"{metabase.rstrip('/')}/dashboard/{id_}"}
                for name, id_ in connection.config.dashboards.items()
            ]
            if metabase
            else [],
        }

    def run_link(
        self, miroir: ResolvedMiroir, backend_run_id: str | None
    ) -> str | None:
        ui = miroir.links.prefect_ui
        if ui and backend_run_id:
            return f"{ui.rstrip('/')}/runs/flow-run/{backend_run_id}"
        return None

    def overview(self) -> dict[str, Any]:
        miroir = self.miroir()
        return {
            "miroir": miroir.id,
            "revision": miroir.revision,
            "connections": [self._summary(miroir, c) for c in miroir.connections],
        }

    def _summary(self, miroir: ResolvedMiroir, connection: ResolvedConnection) -> dict:
        from src.core.analysis import analysis_plan

        status = self.store.connection_status(miroir.id, connection.config.id)
        plan = analysis_plan(connection)
        if plan is None and status["analysis"] != "no_data":
            # Loaded data no model reads is not "stale": nothing will ever build it.
            status["analysis"] = "not_modelled"
            status["analysis_detail"] = "integration has no models"
        elif plan is not None and status["analysis"] == "current":
            built = status["last_build"] or {}
            if (
                built.get("config_revision") != connection.revision
                or built.get("transformation_detail") != plan.receipt
            ):
                status["analysis"] = "stale"
                status["analysis_detail"] = (
                    "Rebuild analysis to apply the current settings and models"
                )
        last = status["last_attempt"]
        if last is not None:
            last["run_url"] = self.run_link(miroir, last.get("backend_run_id"))
        applied = self.store.last_executed_revision(miroir.id, connection.config.id)
        return {
            **connection.summary(),
            "status": status,
            "analysis": plan.summary() if plan else None,
            # Saved configuration vs the configuration its last execution used.
            "configuration": "never_run"
            if applied is None
            else "applied"
            if applied == connection.revision
            else "saved_not_applied",
            "can_sync": connection.can_sync,
            "sync_label": "Sync now"
            if connection.integration.fetch is not None
            else "Scan folder",
            "accepts_files": connection.integration.accepts_files,
            "delivery": self.delivery_status(connection.config.id, miroir),
            # The connector's formats; uploads and the receiving URL use the first.
            "formats": connection.integration.describe()["formats"],
            "title": connection.integration.title,
            "credentials": self._credentials(miroir, connection),
            "credential_status": self.store.credential_status(
                miroir.id, connection.config.id
            ),
            **self._links(miroir, connection),
        }

    def _credentials(
        self, miroir: ResolvedMiroir, connection: ResolvedConnection
    ) -> list[dict[str, Any]]:
        """Write-only view: where each credential lives and whether it is set."""
        stored = self.store.secret_metadata(miroir.id, connection.config.id)
        result = []
        for credential in connection.integration.credentials:
            reference = connection.config.credentials[credential.name]
            item: dict[str, Any] = {
                "name": credential.name,
                "label": credential.label,
                "help": credential.help,
                "provider": reference.provider,
            }
            if reference.provider == "miroir":
                item["set"] = credential.name in stored
                item["updated_at"] = stored.get(credential.name, {}).get("updated_at")
            else:
                item["external_key"] = reference.key
            result.append(item)
        return result

    def connection(self, connection_id: str) -> dict[str, Any]:
        miroir = self.miroir()
        connection = self._connection(miroir, connection_id)
        operations = self.store.recent_operations(miroir.id, connection_id)
        artifacts = self.artifacts(connection_id, miroir)
        names = {artifact["id"]: artifact["original_name"] for artifact in artifacts}
        for operation in operations:
            operation["run_url"] = self.run_link(miroir, operation["backend_run_id"])
            operation["artifact_name"] = names.get(operation["artifact_id"])
        return {
            **self._summary(miroir, connection),
            "operations": operations,
            "artifacts": artifacts,
        }

    def artifacts(
        self, connection_id: str, miroir: ResolvedMiroir | None = None
    ) -> list:
        miroir = miroir or self.miroir()
        self._connection(miroir, connection_id)
        return [
            {**artifact, "imports": self.store.artifact_imports(artifact["id"])}
            for artifact in reversed(
                self.store.list_artifacts(miroir.id, connection_id)
            )
        ]

    def request_sync(self, connection_id: str, origin: Any = "request") -> str:
        from src.core.runner import request_sync

        self.prepare_connection(connection_id)
        operation = request_sync(
            self.miroir(), connection_id, store=self.store, origin=origin
        )
        self.executor.submit(operation.id)
        return operation.id

    def submit_file(
        self,
        connection_id: str,
        path: Path,
        options: dict[str, Any] | None = None,
        *,
        import_now: bool = False,
        original_name: str | None = None,
        origin: Any = "request",
        input_format: str | None = None,
    ) -> dict[str, Any]:
        """Register one uploaded or received file, and optionally import it.

        `options` are the format's import options; received files take its
        defaults. `origin` is the operation origin ("webhook" for the URL).
        """
        from src.core.artifacts import submit_artifact
        from src.core.runner import AlreadyImported

        miroir = self.miroir()
        connection = self._connection(miroir, connection_id)
        try:
            file_format = connection.integration.format(input_format)
        except KeyError:
            raise OperationError("This integration does not accept that file") from None
        received: ArtifactOrigin = "webhook" if origin == "webhook" else "upload"
        artifact, created = submit_artifact(
            miroir,
            connection,
            path,
            options,
            store=self.store,
            root=self.artifact_root,
            original_name=original_name
            or (file_format.received_name if received == "webhook" else None),
            origin=received,
            input_format=file_format.id,
        )
        result: dict[str, Any] = {"artifact": artifact, "new": created}
        if import_now:
            try:
                result["operation_id"] = self.request_import(
                    connection_id, artifact["id"], origin=origin
                )
            except AlreadyImported as error:
                result["already_imported"] = error.operation_id
        return result

    def delivery_status(
        self, connection_id: str, miroir: ResolvedMiroir | None = None
    ) -> dict[str, Any]:
        miroir = miroir or self.miroir()
        connection = self._connection(miroir, connection_id)
        enabled = connection.integration.accepts_files
        stored = (
            self.store.delivery_token(miroir.id, connection_id) if enabled else None
        )
        return {
            "enabled": stored is not None,
            "updated_at": stored["updated_at"].isoformat() if stored else None,
            "last_received_at": self.store.last_artifact_received(
                miroir.id, connection_id
            )
            if enabled
            else None,
        }

    def create_delivery_token(self, connection_id: str) -> str:
        miroir = self.miroir()
        connection = self._connection(miroir, connection_id)
        if not connection.integration.accepts_files:
            raise OperationError("Integration does not accept files")
        self.prepare_connection(connection_id)
        token = random_secrets.token_urlsafe(32)
        self.store.set_delivery_token(
            miroir.id, connection_id, hashlib.sha256(token.encode()).hexdigest()
        )
        return token

    def revoke_delivery_token(self, connection_id: str) -> None:
        miroir = self.miroir()
        self._connection(miroir, connection_id)
        self.store.revoke_delivery_token(miroir.id, connection_id)

    def verify_delivery_token(self, connection_id: str, token: str) -> bool:
        if not token:
            return False
        miroir = self.miroir()
        try:
            connection = self._connection(miroir, connection_id)
        except OperationError:
            return False
        if not connection.integration.accepts_files:
            return False
        stored = self.store.delivery_token(miroir.id, connection_id)
        return stored is not None and hmac.compare_digest(
            stored["token_hash"], hashlib.sha256(token.encode()).hexdigest()
        )

    def request_import(
        self,
        connection_id: str,
        artifact_id: str,
        replay: bool = False,
        origin: Any = "request",
    ) -> str:
        from src.core.runner import request_import

        self.prepare_connection(connection_id)
        operation = request_import(
            self.miroir(),
            connection_id,
            artifact_id,
            store=self.store,
            origin=origin,
            replay=replay,
        )
        self.executor.submit(operation.id)
        return operation.id

    def request_models(self, connection_id: str) -> str:
        from src.core.runner import request_models

        miroir = self.miroir()
        self.prepare_connection(connection_id)
        operation = request_models(
            miroir, connection_id, store=self.store, origin="request"
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
            load_miroir(self.config_path)
            error = None
        except ConfigurationError as problem:
            error = str(problem)
        return {
            "text": text,
            "file_revision": file_revision(self.config_path),
            "error": error,
        }

    def save_configuration(self, text: str, file_revision: str) -> ResolvedMiroir:
        return save_miroir(self.config_path, text, file_revision)

    # ---- Connections and credentials ------------------------------------------

    def integrations(self) -> list[dict[str, Any]]:
        from src.integrations.registry import INTEGRATIONS

        return [
            {
                **definition.describe(),
                # Every setting describes the source; delivery lives elsewhere.
                "connection_fields": list(definition.settings_model.model_fields),
            }
            for definition in INTEGRATIONS.values()
        ]

    def connection_settings(self, connection_id: str) -> dict[str, Any]:
        import json

        from src.core.config import parse_miroir

        raw, revision = self._raw_config()
        miroir = parse_miroir(json.dumps(raw))
        connection = self._connection(miroir, connection_id)
        definition = next(
            i for i in self.integrations() if i["name"] == connection.integration.name
        )
        return {
            "connection": self.connection(connection_id),
            "integration": definition,
            "values": connection.settings.model_dump(mode="json"),
            "analysis_values": connection.config.analysis.model_dump(mode="json"),
            "folder_values": connection.config.folder.model_dump(mode="json")
            if connection.config.folder
            else None,
            "file_revision": revision,
        }

    def setup_method(self, integration: str, method: str = "") -> dict[str, Any]:
        definition = next(
            (i for i in self.integrations() if i["name"] == integration), None
        )
        if definition is None:
            raise OperationError("Unknown integration")
        methods = definition["delivery_methods"]
        selected = (
            next((m for m in methods if m["id"] == (method or methods[0]["id"])), None)
            if methods
            else None
        )
        if selected is None:
            raise OperationError(
                "This integration does not support that delivery method"
            )
        return dict(selected)

    def update_connection_settings(
        self, connection_id: str, settings: dict[str, Any], expected_revision: str
    ) -> None:
        from src.core.config import ConfigurationConflict

        if not expected_revision:
            raise OperationError("Reload settings before saving")
        raw, revision = self._raw_config()
        if revision != expected_revision:
            raise ConfigurationConflict(
                "Configuration changed since you loaded it; reload settings before saving"
            )
        self._connection(self.miroir(), connection_id)
        target = next(c for c in raw["connections"] if c["id"] == connection_id)
        updated = {**target["settings"], **settings}
        target["settings"] = {
            key: value for key, value in updated.items() if value is not None
        }
        self._write_config(raw, expected_revision)

    def update_folder(
        self, connection_id: str, path: str | None, expected_revision: str
    ) -> None:
        """Set or clear the watched folder; other folder choices are kept."""
        from src.core.config import ConfigurationConflict

        if not expected_revision:
            raise OperationError("Reload settings before saving")
        raw, revision = self._raw_config()
        if revision != expected_revision:
            raise ConfigurationConflict(
                "Configuration changed; reload settings before saving"
            )
        connection = self._connection(self.miroir(), connection_id)
        if path and not connection.integration.accepts_files:
            raise OperationError("This integration reads no files")
        target = next(c for c in raw["connections"] if c["id"] == connection_id)
        target.get("settings", {}).pop("data_dir", None)
        if path:
            target["folder"] = {**target.get("folder", {}), "path": path}
        else:
            target.pop("folder", None)
        self._write_config(raw, expected_revision)

    def update_analysis_settings(
        self, connection_id: str, settings: dict[str, Any], expected_revision: str
    ) -> None:
        from src.core.config import ConfigurationConflict

        if not expected_revision:
            raise OperationError("Reload settings before saving")
        raw, revision = self._raw_config()
        if revision != expected_revision:
            raise ConfigurationConflict(
                "Configuration changed; reload settings before saving"
            )
        self._connection(self.miroir(), connection_id)
        target = next(c for c in raw["connections"] if c["id"] == connection_id)
        target["analysis"] = {**target.get("analysis", {}), **settings}
        self._write_config(raw, expected_revision)

    def prepare_connection(self, connection_id: str) -> None:
        """Make this connection executable without applying other connections' drift."""
        miroir = self.miroir()
        self._connection(miroir, connection_id)
        if self.orchestrator is None:
            return  # The local executor needs no deployment.
        try:
            changes = [
                change
                for change in self.orchestrator.plan(miroir)
                if change.get("connection_id") == connection_id
                and change["action"] in ("create", "update")
            ]
            if changes:
                self.orchestrator.apply(miroir, changes)
        except OperationError as error:
            raise OperationError(f"Your connection is saved. {error}") from error

    def _raw_config(self) -> tuple[dict[str, Any], str]:
        import json

        data = self.config_path.read_bytes()
        return json.loads(data), hashlib.sha256(data).hexdigest()

    def _write_config(self, raw: dict[str, Any], revision: str) -> ResolvedMiroir:
        import json

        return save_miroir(self.config_path, json.dumps(raw, indent=2) + "\n", revision)

    def create_connection(
        self,
        integration: str,
        connection_id: str,
        settings: dict[str, Any],
        credentials: dict[str, str],
        *,
        delivery_method: str | None = None,
        folder_path: str | None = None,
    ) -> str:
        """Add a connection whose credentials are stored by the miroir.

        Validates the new configuration first, stores secrets, then saves the
        configuration; if saving fails, the just-stored secrets are removed.
        """
        import json

        from src.core.config import ConfigurationError, parse_miroir
        from src.integrations.registry import INTEGRATIONS

        definition = INTEGRATIONS.get(integration)
        if definition is None:
            raise OperationError("Unknown integration")
        method = (
            self.setup_method(integration, delivery_method)
            if delivery_method is not None
            else None
        )
        if method is not None and method["id"] == "folder" and not folder_path:
            raise OperationError("Provide the server folder to watch")
        if folder_path and (method is None or method["id"] != "folder"):
            raise OperationError("A server folder belongs to the folder method")
        if set(credentials) != set(definition.credential_names):
            raise OperationError("Provide every credential the integration needs")
        secrets = self.secrets
        if credentials and secrets is None:
            raise SecretResolutionError(
                "Set MIROIR_SECRET_KEY to store credentials in the miroir"
            )
        raw, revision = self._raw_config()
        raw.setdefault("connections", []).append(
            {
                "id": connection_id,
                "integration": integration,
                "settings": settings,
                "credentials": {name: {"provider": "miroir"} for name in credentials},
                **({"folder": {"path": folder_path}} if folder_path else {}),
            }
        )
        miroir = parse_miroir(json.dumps(raw))  # validate before storing anything
        connection = self._connection(miroir, connection_id)
        if self.store.secret_metadata(miroir.id, connection_id):
            raise ConfigurationError("Secrets already exist for this connection ID")
        try:
            for name, value in credentials.items():
                assert secrets is not None  # checked above
                secrets.put(connection, name, value)
            self._write_config(raw, revision)
        except BaseException:
            self.store.delete_secrets(miroir.id, connection_id)
            raise
        return connection_id

    def set_credential(self, connection_id: str, name: str, value: str) -> None:
        miroir = self.miroir()
        connection = self._connection(miroir, connection_id)
        reference = connection.config.credentials.get(name)
        if reference is None:
            raise OperationError("Unknown credential for this connection")
        if reference.provider != "miroir":
            raise OperationError(
                f"This credential comes from {reference.provider} ({reference.key}); "
                "change it there, or switch it to the miroir in the configuration"
            )
        if self.secrets is None:
            raise SecretResolutionError(
                "Set MIROIR_SECRET_KEY to store credentials in the miroir"
            )
        self.secrets.put(connection, name, value)

    def check_connection(self, connection_id: str) -> dict[str, Any]:
        """Run the integration's check and record the outcome."""
        from src.core.secrets import SecretKeyError
        from src.integrations.checks import CheckFailed

        miroir = self.miroir()
        connection = self._connection(miroir, connection_id)
        check = connection.integration.check
        ok, rejected = False, False
        credentials: dict[str, str] = {}
        try:
            credentials = self.resolver()(connection)
            messages = [check(connection.settings, credentials)] if check else []
            if connection.config.folder is not None:
                messages.append(_check_folder(connection))
            elif not check and connection.integration.accepts_files:
                messages.append("Ready to receive files; upload one to verify it")
            message = "; ".join(messages) or "No check available"
            ok = True
        except (SecretResolutionError, SecretKeyError) as error:
            message = str(error)
        except CheckFailed as error:
            message, rejected = str(error), error.rejected
        for value in credentials.values():
            message = message.replace(value, "***")
        self.store.record_check(miroir.id, connection_id, ok, rejected, message)
        return {"ok": ok, "rejected": rejected, "message": message}

    def remove_connection(self, connection_id: str) -> None:
        """Remove the configuration entry and its stored secrets.

        Loaded data, artifacts, and history are kept: purging them is separate.
        """
        miroir = self.miroir()
        self._connection(miroir, connection_id)
        raw, revision = self._raw_config()
        raw["connections"] = [
            c for c in raw["connections"] if c.get("id") != connection_id
        ]
        self._write_config(raw, revision)
        self.store.delete_secrets(miroir.id, connection_id)
        self.store.revoke_delivery_token(miroir.id, connection_id)

    # ---- Orchestrator -------------------------------------------------------------

    def _orchestrator(self) -> Any:
        if self.orchestrator is None:
            raise OperationError(
                "No orchestrator configured: set deployment.work_pool in the"
                " configuration, or MIROIR_PREFECT_POOL as a default"
            )
        return self.orchestrator

    def deployments_plan(self) -> list[dict[str, Any]]:
        return list(self._orchestrator().plan(self.miroir()))

    def deployments_apply(self) -> list[dict[str, Any]]:
        miroir = self.miroir()
        orchestrator = self._orchestrator()
        changes = orchestrator.plan(miroir)
        orchestrator.apply(miroir, changes)
        return list(changes)

    def refresh(self) -> list[dict[str, Any]]:
        """Reconcile unverified runs with the orchestrator."""
        return list(self._orchestrator().observe(self.miroir()))


def _check_folder(connection: ResolvedConnection) -> str:
    """A watched folder must be readable; matching files are counted, not read."""
    import os

    from src.integrations.checks import CheckFailed

    folder = connection.config.folder
    assert folder is not None and folder.pattern is not None
    root = Path(folder.path)
    if not root.is_dir():
        raise CheckFailed("The watched folder does not exist on this machine")
    if not os.access(root, os.R_OK | os.X_OK):
        raise CheckFailed("The watched folder is not readable")
    count = sum(1 for path in root.glob(folder.pattern) if path.is_file())
    return f"Watched folder is readable; {count} file(s) match {folder.pattern}"
