"""Operation and attempt records: the metadata store behind truthful status.

Execution history stays in Prefect and load state stays in dlt. This store only
maps product identities (workspace, connection, operation) to backend references
and records per-stage outcomes, so status can tell ingestion and analysis apart.
"""

import fcntl
import hashlib
import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import sqlalchemy as sa

SCHEMA_VERSION = 3
POSTGRES_SCHEMA = "workspace_meta"
LOCK_POLL_SECONDS = 1.0

Origin = Literal["local", "request", "prefect"]
Role = Literal["historical", "ongoing"]
# `lost` (the run crashed) and `cancelled` come from reconciling with the
# orchestrator; they are kept apart from `failed`, which the runner recorded itself.
Ingestion = Literal["running", "succeeded", "failed", "blocked", "lost", "cancelled"]
Transformation = Literal["pending", "succeeded", "failed", "skipped", "not_run"]

metadata = sa.MetaData()

schema_info = sa.Table(
    "schema_info",
    metadata,
    sa.Column("version", sa.Integer, nullable=False),
)

operations = sa.Table(
    "operations",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("workspace_id", sa.String(24), nullable=False),
    sa.Column("connection_id", sa.String(24), nullable=False),
    sa.Column("integration", sa.String(24), nullable=False),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("origin", sa.String(16), nullable=False),
    sa.Column("pipeline_name", sa.String(63), nullable=False),
    sa.Column("dataset_name", sa.String(63), nullable=False),
    sa.Column("config_revision", sa.String(64), nullable=False),
    sa.Column("code_revision", sa.String(64), nullable=False),
    # Prefect retries reuse the flow run ID; it identifies one operation.
    sa.Column("backend_run_id", sa.String(64), unique=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    # Imports only (schema version 2): what was parsed, by which parser, how.
    sa.Column("artifact_id", sa.String(36)),
    sa.Column("parser_version", sa.Integer),
    sa.Column("options", sa.Text),
)

# Columns added to `operations` by version 2, as portable DDL fragments.
OPERATION_COLUMNS_V2 = (
    "artifact_id VARCHAR(36)",
    "parser_version INTEGER",
    "options TEXT",
)

artifacts = sa.Table(
    "artifacts",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("workspace_id", sa.String(24), nullable=False),
    sa.Column("connection_id", sa.String(24), nullable=False),
    sa.Column("sha256", sa.String(64), nullable=False),
    sa.Column("size", sa.BigInteger, nullable=False),
    sa.Column("input_format", sa.String(64), nullable=False),
    sa.Column("role", sa.String(16), nullable=False),
    sa.Column("original_name", sa.String(255), nullable=False),
    sa.Column("storage_path", sa.Text, nullable=False),
    sa.Column("retention", sa.String(16), nullable=False),
    sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    # Identical bytes from two accounts are two artifacts.
    sa.UniqueConstraint("workspace_id", "connection_id", "sha256"),
)

attempts = sa.Table(
    "attempts",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("operation_id", sa.ForeignKey("operations.id"), nullable=False),
    sa.Column("number", sa.Integer, nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("ingestion", sa.String(16), nullable=False),
    sa.Column("ingested_at", sa.DateTime(timezone=True)),
    sa.Column("load_ids", sa.Text),
    sa.Column("transformation", sa.String(16), nullable=False),
    sa.Column("transformation_detail", sa.String(200)),
    sa.Column("transformed_at", sa.DateTime(timezone=True)),
    sa.Column("error_type", sa.String(200)),
    sa.Column("error_message", sa.Text),
    # Version 3: why it failed, when known (e.g. "credentials_rejected").
    sa.Column("failure_kind", sa.String(32)),
    sa.UniqueConstraint("operation_id", "number"),
)

secrets = sa.Table(
    "secrets",
    metadata,
    sa.Column("workspace_id", sa.String(24), primary_key=True),
    sa.Column("connection_id", sa.String(24), primary_key=True),
    sa.Column("name", sa.String(64), primary_key=True),
    # Fernet token; the key is never stored here.
    sa.Column("ciphertext", sa.LargeBinary, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

connection_checks = sa.Table(
    "connection_checks",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("workspace_id", sa.String(24), nullable=False),
    sa.Column("connection_id", sa.String(24), nullable=False),
    sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("ok", sa.Boolean, nullable=False),
    sa.Column("rejected", sa.Boolean, nullable=False),
    sa.Column("message", sa.String(500), nullable=False),
)


class OperationError(RuntimeError):
    """An operation cannot be registered, adopted, or started."""


class ConnectionBusy(OperationError):
    """Another execution holds the connection's dlt state."""


@dataclass(frozen=True)
class Operation:
    id: str
    workspace_id: str
    connection_id: str
    integration: str
    kind: str
    origin: str
    pipeline_name: str
    dataset_name: str
    config_revision: str
    code_revision: str
    backend_run_id: str | None
    created_at: datetime
    artifact_id: str | None = None
    parser_version: int | None = None
    options: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes; everything written here is UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class MetadataStore:
    def __init__(self, url: str):
        engine = sa.create_engine(url)
        if engine.dialect.name == "postgresql":
            engine = engine.execution_options(
                schema_translate_map={None: POSTGRES_SCHEMA}
            )
        elif engine.dialect.name != "sqlite":
            raise OperationError("Metadata store must be SQLite or PostgreSQL")
        self.engine = engine
        self.url = engine.url

    def migrate(self) -> None:
        """Create the schema, or upgrade it in one transaction."""
        postgres = self.engine.dialect.name == "postgresql"
        with self.engine.begin() as db:
            if postgres:
                db.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {POSTGRES_SCHEMA}"))
            existing = sa.inspect(db).has_table(
                "schema_info", schema=POSTGRES_SCHEMA if postgres else None
            )
            version = (
                db.execute(sa.select(schema_info.c.version)).scalar()
                if existing
                else None
            )
            if version is not None and not 1 <= version <= SCHEMA_VERSION:
                raise OperationError(
                    f"Metadata schema version {version} is not supported"
                )
            # Adds tables that do not exist yet; never alters existing ones.
            metadata.create_all(db)
            if version is None:
                db.execute(schema_info.insert().values(version=SCHEMA_VERSION))
                return
            prefix = f"{POSTGRES_SCHEMA}." if postgres else ""
            if version < 2:
                for column in OPERATION_COLUMNS_V2:
                    db.execute(
                        sa.text(f"ALTER TABLE {prefix}operations ADD COLUMN {column}")
                    )
            if version < 3:
                db.execute(
                    sa.text(
                        f"ALTER TABLE {prefix}attempts ADD COLUMN failure_kind VARCHAR(32)"
                    )
                )
            db.execute(schema_info.update().values(version=SCHEMA_VERSION))

    def create_operation(self, **values: Any) -> Operation:
        values = {"id": str(uuid.uuid4()), "created_at": _now(), **values}
        with self.engine.begin() as db:
            db.execute(operations.insert().values(**values))
        return self.get_operation(values["id"])

    def get_operation(self, operation_id: str) -> Operation:
        with self.engine.connect() as db:
            row = db.execute(
                operations.select().where(operations.c.id == operation_id)
            ).first()
        if row is None:
            raise OperationError("Unknown operation")
        data = row._asdict()
        data["created_at"] = _aware(data["created_at"])
        return Operation(**data)

    def find_by_backend_run(self, backend_run_id: str) -> Operation | None:
        with self.engine.connect() as db:
            operation_id = db.execute(
                sa.select(operations.c.id).where(
                    operations.c.backend_run_id == backend_run_id
                )
            ).scalar()
        return None if operation_id is None else self.get_operation(operation_id)

    def bind_backend_run(self, operation_id: str, backend_run_id: str) -> None:
        with self.engine.begin() as db:
            updated = db.execute(
                operations.update()
                .where(operations.c.id == operation_id)
                .where(
                    sa.or_(
                        operations.c.backend_run_id.is_(None),
                        operations.c.backend_run_id == backend_run_id,
                    )
                )
                .values(backend_run_id=backend_run_id)
            ).rowcount
        if updated != 1:
            raise OperationError("Operation is bound to a different backend run")

    def start_attempt(self, operation_id: str, ingestion: Ingestion) -> tuple[int, int]:
        """Insert the next attempt; returns its (row ID, attempt number)."""
        with self.engine.begin() as db:
            number = (
                1
                + db.execute(
                    sa.select(
                        sa.func.coalesce(sa.func.max(attempts.c.number), 0)
                    ).where(attempts.c.operation_id == operation_id)
                ).scalar_one()
            )
            row = db.execute(
                attempts.insert().values(
                    operation_id=operation_id,
                    number=number,
                    started_at=_now(),
                    ingestion=ingestion,
                    transformation="pending" if ingestion == "running" else "not_run",
                )
            ).inserted_primary_key
        assert row is not None
        return int(row[0]), number

    def update_attempt(self, attempt_id: int, **values: Any) -> None:
        if "load_ids" in values:
            values["load_ids"] = json.dumps(values["load_ids"])
        if values.get("ingestion") == "succeeded":
            values["ingested_at"] = _now()
        if values.get("transformation") == "succeeded":
            values["transformed_at"] = _now()
        with self.engine.begin() as db:
            db.execute(
                attempts.update().where(attempts.c.id == attempt_id).values(**values)
            )

    def attempts(self, operation_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as db:
            rows = db.execute(
                attempts.select()
                .where(attempts.c.operation_id == operation_id)
                .order_by(attempts.c.number)
            ).all()
        return [_attempt(row) for row in rows]

    def connection_status(self, workspace_id: str, connection_id: str) -> dict:
        """Project recorded evidence into a status summary for one connection.

        `running` attempts are not reconciled with Prefect yet: a crashed or
        cancelled worker leaves them running here, so they are reported unverified.
        """
        scope = sa.and_(
            operations.c.workspace_id == workspace_id,
            operations.c.connection_id == connection_id,
        )
        joined = attempts.join(operations)
        with self.engine.connect() as db:
            latest = db.execute(
                sa.select(attempts, operations.c.origin, operations.c.backend_run_id)
                .select_from(joined)
                .where(scope)
                .order_by(attempts.c.id.desc())
                .limit(1)
            ).first()
            ingested = db.execute(
                sa.select(sa.func.max(attempts.c.ingested_at))
                .select_from(joined)
                .where(scope)
            ).scalar()
            transformed = db.execute(
                sa.select(sa.func.max(attempts.c.transformed_at))
                .select_from(joined)
                .where(scope)
            ).scalar()
            pending = db.execute(
                sa.select(sa.func.count())
                .select_from(operations.outerjoin(attempts))
                .where(scope, attempts.c.id.is_(None))
            ).scalar_one()
        ingested, transformed = _aware(ingested), _aware(transformed)
        if ingested is None:
            analysis = "no_data"
        elif transformed is not None and transformed >= ingested:
            analysis = "current"
        else:
            analysis = "stale"
        last = None
        if latest is not None:
            last = _attempt(latest)
            last["origin"] = latest.origin
            last["backend_run_id"] = latest.backend_run_id
            last["verified"] = last["ingestion"] != "running"
        return {
            "connection": connection_id,
            "last_attempt": last,
            "last_ingestion_success": _iso(ingested),
            "last_transformation_success": _iso(transformed),
            "analysis": analysis,
            "requested_not_started": pending,
        }

    def last_executed_revision(
        self, workspace_id: str, connection_id: str
    ) -> str | None:
        """Connection configuration revision used by the latest attempt that ran."""
        with self.engine.connect() as db:
            revision: str | None = db.execute(
                sa.select(operations.c.config_revision)
                .select_from(attempts.join(operations))
                .where(
                    operations.c.workspace_id == workspace_id,
                    operations.c.connection_id == connection_id,
                    attempts.c.ingestion != "blocked",
                )
                .order_by(attempts.c.id.desc())
                .limit(1)
            ).scalar()
        return revision

    def recent_operations(
        self, workspace_id: str, connection_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Newest operations first, each with its attempts (oldest first)."""
        with self.engine.connect() as db:
            rows = db.execute(
                operations.select()
                .where(
                    operations.c.workspace_id == workspace_id,
                    operations.c.connection_id == connection_id,
                )
                .order_by(operations.c.created_at.desc())
                .limit(limit)
            ).all()
        result = []
        for row in rows:
            data = row._asdict()
            data["created_at"] = _iso(_aware(data["created_at"]))
            data["attempts"] = self.attempts(data["id"])
            result.append(data)
        return result

    def put_secret(
        self, workspace_id: str, connection_id: str, name: str, ciphertext: bytes
    ) -> None:
        key = {
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "name": name,
        }
        now = _now()
        with self.engine.begin() as db:
            updated = db.execute(
                secrets.update()
                .where(*(secrets.c[k] == v for k, v in key.items()))
                .values(ciphertext=ciphertext, updated_at=now)
            ).rowcount
            if not updated:
                db.execute(
                    secrets.insert().values(
                        **key, ciphertext=ciphertext, created_at=now, updated_at=now
                    )
                )

    def get_secret(
        self, workspace_id: str, connection_id: str, name: str
    ) -> bytes | None:
        with self.engine.connect() as db:
            value: bytes | None = db.execute(
                sa.select(secrets.c.ciphertext).where(
                    secrets.c.workspace_id == workspace_id,
                    secrets.c.connection_id == connection_id,
                    secrets.c.name == name,
                )
            ).scalar()
        return value

    def secret_metadata(self, workspace_id: str, connection_id: str) -> dict[str, dict]:
        """Name -> timestamps for stored secrets. Never returns ciphertext."""
        with self.engine.connect() as db:
            rows = db.execute(
                sa.select(
                    secrets.c.name, secrets.c.created_at, secrets.c.updated_at
                ).where(
                    secrets.c.workspace_id == workspace_id,
                    secrets.c.connection_id == connection_id,
                )
            ).all()
        return {
            row.name: {
                "created_at": _iso(_aware(row.created_at)),
                "updated_at": _iso(_aware(row.updated_at)),
            }
            for row in rows
        }

    def delete_secrets(
        self, workspace_id: str, connection_id: str, names: list[str] | None = None
    ) -> int:
        query = secrets.delete().where(
            secrets.c.workspace_id == workspace_id,
            secrets.c.connection_id == connection_id,
        )
        if names is not None:
            query = query.where(secrets.c.name.in_(names))
        with self.engine.begin() as db:
            return int(db.execute(query).rowcount)

    def all_secrets(self) -> list[tuple[dict[str, str], bytes]]:
        """Every stored secret's identity and ciphertext, for key rotation."""
        with self.engine.connect() as db:
            rows = db.execute(secrets.select()).all()
        return [
            (
                {
                    "workspace_id": row.workspace_id,
                    "connection_id": row.connection_id,
                    "name": row.name,
                },
                row.ciphertext,
            )
            for row in rows
        ]

    def record_check(
        self,
        workspace_id: str,
        connection_id: str,
        ok: bool,
        rejected: bool,
        message: str,
    ) -> None:
        with self.engine.begin() as db:
            db.execute(
                connection_checks.insert().values(
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    checked_at=_now(),
                    ok=ok,
                    rejected=rejected,
                    message=message[:500],
                )
            )

    def credential_status(
        self, workspace_id: str, connection_id: str
    ) -> dict[str, Any]:
        """Latest evidence about the credentials: a check or a run.

        `rejected` if the latest evidence is a refused check or a run that failed
        with credentials rejected; `ok` if it is a passing check or a successful
        load; `unknown` before any evidence.
        """
        scope_checks = sa.and_(
            connection_checks.c.workspace_id == workspace_id,
            connection_checks.c.connection_id == connection_id,
        )
        with self.engine.connect() as db:
            check = db.execute(
                connection_checks.select()
                .where(scope_checks)
                .order_by(connection_checks.c.id.desc())
                .limit(1)
            ).first()
            run = db.execute(
                sa.select(
                    attempts.c.started_at, attempts.c.ingestion, attempts.c.failure_kind
                )
                .select_from(attempts.join(operations))
                .where(
                    operations.c.workspace_id == workspace_id,
                    operations.c.connection_id == connection_id,
                    sa.or_(
                        attempts.c.ingestion == "succeeded",
                        attempts.c.failure_kind == "credentials_rejected",
                    ),
                )
                .order_by(attempts.c.id.desc())
                .limit(1)
            ).first()
        evidence: list[tuple[datetime, str, str, str]] = []
        if check is not None:
            state = (
                "ok" if check.ok else "rejected" if check.rejected else "check_failed"
            )
            evidence.append((_utc(check.checked_at), state, check.message, "check"))
        if run is not None:
            state = "ok" if run.ingestion == "succeeded" else "rejected"
            message = "Last load succeeded" if state == "ok" else "A run was refused"
            evidence.append((_utc(run.started_at), state, message, "run"))
        if not evidence:
            return {"state": "unknown", "message": None, "at": None, "source": None}
        at, state, message, source = max(evidence, key=lambda item: item[0])
        return {"state": state, "message": message, "at": _iso(at), "source": source}

    def unverified(self, workspace_id: str) -> list[dict[str, Any]]:
        """Submitted work whose outcome only the orchestrator knows.

        Attempts still `running`, and operations bound to a backend run that never
        started an attempt (the run died before registering).
        """
        with self.engine.connect() as db:
            running = db.execute(
                sa.select(
                    attempts.c.id,
                    operations.c.id.label("operation_id"),
                    operations.c.backend_run_id,
                )
                .select_from(attempts.join(operations))
                .where(
                    operations.c.workspace_id == workspace_id,
                    attempts.c.ingestion == "running",
                    operations.c.backend_run_id.is_not(None),
                )
            ).all()
            unstarted = db.execute(
                sa.select(
                    operations.c.id.label("operation_id"), operations.c.backend_run_id
                )
                .select_from(operations.outerjoin(attempts))
                .where(
                    operations.c.workspace_id == workspace_id,
                    operations.c.backend_run_id.is_not(None),
                    attempts.c.id.is_(None),
                )
            ).all()
        return [
            {
                "attempt_id": row.id,
                "operation_id": row.operation_id,
                "backend_run_id": row.backend_run_id,
            }
            for row in running
        ] + [
            {
                "attempt_id": None,
                "operation_id": row.operation_id,
                "backend_run_id": row.backend_run_id,
            }
            for row in unstarted
        ]

    def close_unverified(
        self,
        operation_id: str,
        attempt_id: int | None,
        outcome: Ingestion,
        message: str,
    ) -> None:
        """Record what the orchestrator says happened to a run we never heard back from."""
        if attempt_id is None:
            attempt_id, _ = self.start_attempt(operation_id, outcome)
        self.update_attempt(
            attempt_id,
            ingestion=outcome,
            transformation="not_run",
            error_type="Orchestrator",
            error_message=message,
        )

    def register_artifact(self, **values: Any) -> tuple[dict[str, Any], bool]:
        """Record received bytes; returns (artifact, newly_registered).

        Resubmitting identical bytes to the same connection returns the existing
        artifact. Resubmitting them with a different role is refused: the role
        decides which rows the file contributes.
        """
        scope = sa.and_(
            artifacts.c.workspace_id == values["workspace_id"],
            artifacts.c.connection_id == values["connection_id"],
            artifacts.c.sha256 == values["sha256"],
        )
        with self.engine.begin() as db:
            row = db.execute(artifacts.select().where(scope)).first()
            if row is None:
                values = {"id": str(uuid.uuid4()), "received_at": _now(), **values}
                db.execute(artifacts.insert().values(**values))
                row = db.execute(artifacts.select().where(scope)).one()
                created = True
            else:
                created = False
        artifact = _artifact(row)
        if artifact["role"] != values["role"]:
            raise OperationError(
                f"Identical file was already submitted as {artifact['role']}"
            )
        return artifact, created

    def get_artifact(self, workspace_id: str, artifact_id: str) -> dict[str, Any]:
        with self.engine.connect() as db:
            row = db.execute(
                artifacts.select().where(
                    artifacts.c.workspace_id == workspace_id,
                    artifacts.c.id == artifact_id,
                )
            ).first()
        if row is None:
            raise OperationError("Unknown artifact")
        return _artifact(row)

    def artifact_imports(self, artifact_id: str) -> list[dict[str, Any]]:
        """Every import attempt of one artifact, oldest first."""
        with self.engine.connect() as db:
            rows = db.execute(
                sa.select(
                    attempts,
                    operations.c.parser_version,
                    operations.c.options,
                )
                .select_from(attempts.join(operations))
                .where(operations.c.artifact_id == artifact_id)
                .order_by(attempts.c.id)
            ).all()
        result = []
        for row in rows:
            attempt = _attempt(row)
            attempt["parser_version"] = row.parser_version
            attempt["options"] = json.loads(row.options) if row.options else None
            result.append(attempt)
        return result

    def list_artifacts(self, workspace_id: str, connection_id: str) -> list[dict]:
        with self.engine.connect() as db:
            rows = db.execute(
                artifacts.select()
                .where(
                    artifacts.c.workspace_id == workspace_id,
                    artifacts.c.connection_id == connection_id,
                )
                .order_by(artifacts.c.received_at)
            ).all()
        return [_artifact(row) for row in rows]

    @contextmanager
    def connection_lock(self, pipeline_name: str, wait: float = 0) -> Iterator[None]:
        """Serialize executions sharing one dlt pipeline state, across processes.

        Waits up to `wait` seconds for a running execution to finish, then raises
        ConnectionBusy. Both mechanisms release automatically when the holding
        process dies. PostgreSQL advisory locks cover every worker using the same
        database; the SQLite file lock only covers processes on one host.
        """
        deadline = time.monotonic() + wait

        def retry() -> bool:
            if time.monotonic() >= deadline:
                return False
            time.sleep(LOCK_POLL_SECONDS)
            return True

        if self.engine.dialect.name == "postgresql":
            digest = hashlib.sha256(f"workspace:{pipeline_name}".encode()).digest()
            key = int.from_bytes(digest[:8], "big", signed=True)
            with self.engine.connect() as db:
                while not db.execute(
                    sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
                ).scalar():
                    if not retry():
                        raise ConnectionBusy("Connection is already running")
                try:
                    yield
                finally:
                    db.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                    db.commit()
            return
        database = self.url.database
        if not database or database == ":memory:":
            raise OperationError("Connection locks need a file-backed SQLite store")
        lock_dir = Path(database).with_suffix(".locks")
        lock_dir.mkdir(exist_ok=True)
        with open(lock_dir / f"{pipeline_name}.lock", "w") as handle:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as error:
                    if not retry():
                        raise ConnectionBusy("Connection is already running") from error
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _attempt(row: Any) -> dict[str, Any]:
    data = row._asdict()
    return {
        "operation_id": data["operation_id"],
        "number": data["number"],
        "started_at": _iso(_aware(data["started_at"])),
        "ingestion": data["ingestion"],
        "load_ids": json.loads(data["load_ids"]) if data["load_ids"] else [],
        "transformation": data["transformation"],
        "transformation_detail": data["transformation_detail"],
        "error_type": data["error_type"],
        "error_message": data["error_message"],
        "failure_kind": data.get("failure_kind"),
    }


def _artifact(row: Any) -> dict[str, Any]:
    data: dict[str, Any] = row._asdict()
    data["received_at"] = _iso(_aware(data["received_at"]))
    return data
