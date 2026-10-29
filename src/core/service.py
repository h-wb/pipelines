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
from src.core.operations import MetadataStore, OperationError, Role
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
        from src.core.runner import model_selector

        status = self.store.connection_status(miroir.id, connection.config.id)
        selector, reason = model_selector(connection)
        if selector is None and status["analysis"] != "no_data":
            # Loaded data no model reads is not "stale": nothing will ever build it.
            status["analysis"] = "not_modelled"
            status["analysis_detail"] = reason
        last = status["last_attempt"]
        if last is not None:
            last["run_url"] = self.run_link(miroir, last.get("backend_run_id"))
        applied = self.store.last_executed_revision(miroir.id, connection.config.id)
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
                "incremental_sync" in connection.integration.capabilities
                or (
                    "directory_import" in connection.integration.capabilities
                    and getattr(connection.settings, "data_dir", None)
                )
            ),
            "sync_label": "Sync now"
            if "incremental_sync" in connection.integration.capabilities
            else "Scan folder",
            "accepts_files": "artifact_import" in connection.integration.capabilities,
            "delivery": self.delivery_status(connection.config.id, miroir),
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

        operation = request_sync(
            self.miroir(), connection_id, store=self.store, origin=origin
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
        origin: Any = "request",
    ) -> dict[str, Any]:
        from src.core.artifacts import submit_artifact
        from src.core.runner import AlreadyImported

        miroir = self.miroir()
        artifact, created = submit_artifact(
            miroir,
            self._connection(miroir, connection_id),
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
        enabled = "artifact_import" in connection.integration.capabilities
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
        if "artifact_import" not in connection.integration.capabilities:
            raise OperationError("Integration does not accept files")
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
        if "artifact_import" not in connection.integration.capabilities:
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
                "name": definition.name,
                "title": definition.title,
                "settings_schema": definition.settings_model.model_json_schema(),
                "credentials": [
                    {"name": f.name, "label": f.label, "help": f.help}
                    for f in definition.credentials
                ],
                "capabilities": list(definition.capabilities),
            }
            for definition in INTEGRATIONS.values()
        ]

    def _raw_config(self) -> tuple[dict[str, Any], str]:
        import json

        revision = file_revision(self.config_path)
        return json.loads(self.config_path.read_text(encoding="utf-8")), revision

    def _write_config(self, raw: dict[str, Any], revision: str) -> ResolvedMiroir:
        import json

        return save_miroir(self.config_path, json.dumps(raw, indent=2) + "\n", revision)

    def create_connection(
        self,
        integration: str,
        connection_id: str,
        settings: dict[str, Any],
        credentials: dict[str, str],
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
            message = (
                check(connection.settings, credentials)
                if check
                else "No check available"
            )
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
