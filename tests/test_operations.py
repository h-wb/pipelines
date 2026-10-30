"""Operation tracking and the shared runner, against synthetic inputs.

HTTP is intercepted, tokens are fake, and destinations and metadata stores are
temporary files. Postgres advisory locks and Prefect execution are not covered.
"""

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import dlt
import duckdb
import requests
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

from src.cli.main import main
from src.core.analysis import analysis_plan
from src.core.config import metadata_url, parse_miroir
from src.core.operations import (
    SCHEMA_VERSION,
    ConnectionBusy,
    MetadataStore,
    OperationError,
    delivery_tokens,
    schema_info,
)
from src.core.operations import (
    attempts as attempt_table,
)
from src.core.runner import (
    IngestionFailed,
    execute_operation,
    request_models,
    run_connection,
)
from src.core.secrets import SecretResolutionError, resolve_credentials
from tests.test_miroir import example

TOKEN = "token-SENSITIVE"


def listens_response(session, request, **kwargs):
    response = requests.Response()
    response.status_code = 200
    response.request = request
    response.url = request.url
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(
        {
            "payload": {
                "listens": [
                    {
                        "listened_at": 1710000000,
                        "recording_msid": "msid",
                        "track_metadata": {"track_name": "track"},
                    }
                ]
            }
        }
    ).encode()
    return response


def leaking_failure(session, request, **kwargs):
    raise requests.ConnectionError(f"refused {request.headers['Authorization']}")


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.open_store("meta.sqlite")
        self.store.migrate()
        # Refused requests would otherwise be retried with backoff.
        retries = patch.dict(os.environ, {"RUNTIME__REQUEST_MAX_ATTEMPTS": "1"})
        retries.start()
        self.addCleanup(retries.stop)
        self.database = self.root / "warehouse.duckdb"

    def open_store(self, name):
        store = MetadataStore(f"sqlite:///{self.root / name}")
        self.addCleanup(store.engine.dispose)
        return store

    def miroir(self, legacy_binding=False, second_account=False):
        config = example()
        config["connections"] = config["connections"][:1]
        if legacy_binding:
            config["connections"][0]["binding"] = {
                "pipeline_name": "listenbrainz",
                "dataset_name": "listenbrainz_data",
            }
        if second_account:
            other = copy.deepcopy(config["connections"][0])
            other["id"] = "other_music"
            config["connections"].append(other)
        return parse_miroir(json.dumps(config))

    def run_music(self, miroir, send=listens_response, store=None, **kwargs):
        kwargs.setdefault("origin", "local")
        with patch.object(requests.Session, "send", send):
            return run_connection(
                miroir,
                kwargs.pop("connection_id", "music"),
                store=store or self.store,
                destination=dlt.destinations.duckdb(credentials=str(self.database)),
                resolve_credentials=lambda connection: {"access_token": TOKEN},
                pipelines_dir=str(self.root / "state"),
                **kwargs,
            )

    def rows(self, dataset):
        with duckdb.connect(str(self.database)) as db:
            return db.execute(f'SELECT count(*) FROM "{dataset}".listens').fetchone()[0]

    def status(self, miroir, connection="music"):
        return self.store.connection_status(miroir.id, connection)

    def analysis(self, miroir, analysis="music"):
        """The store's view; the service also compares the plan receipt."""
        return self.store.analysis_status(miroir.id, analysis, [analysis])

    def test_sync_without_transform_records_load_and_keeps_analysis_stale(self):
        miroir = self.miroir()
        outcome = self.run_music(miroir)
        self.assertEqual(
            (outcome.ingestion, outcome.transformation), ("succeeded", "skipped")
        )
        self.assertTrue(outcome.load_ids)
        operation = self.store.get_operation(outcome.operation_id)
        self.assertEqual(operation.config_revision, miroir.connections[0].revision)
        self.assertEqual(
            operation.dataset_name, miroir.connections[0].binding.dataset_name
        )
        status = self.status(miroir)
        self.assertEqual(status["last_attempt"]["load_ids"], list(outcome.load_ids))
        self.assertEqual(
            status["last_attempt"]["transformation_detail"], "no transform configured"
        )
        # Loaded but never modelled: analysis does not reflect this data.
        self.assertEqual(self.analysis(miroir)["state"], "stale")

    def test_model_failure_keeps_load_and_marks_analysis_stale(self):
        miroir = self.miroir(legacy_binding=True)
        selectors = []
        self.run_music(miroir, transform=selectors.append)
        self.assertEqual(selectors, [analysis_plan(miroir.analyses[0])])
        self.assertEqual(self.analysis(miroir)["state"], "current")

        def failing(selector):
            raise RuntimeError(f"dbt failed near {TOKEN}")

        outcome = self.run_music(miroir, transform=failing)
        self.assertEqual(
            (outcome.ingestion, outcome.transformation), ("succeeded", "failed")
        )
        status = self.status(miroir)
        self.assertEqual(self.analysis(miroir)["state"], "stale")
        self.assertNotIn(TOKEN, json.dumps([status, self.analysis(miroir)]))
        self.assertEqual(self.rows("listenbrainz_data"), 1)

    def test_model_rebuild_uses_existing_data_without_source_credentials(self):
        miroir = self.miroir()
        with self.assertRaisesRegex(OperationError, "Import or sync"):
            request_models(miroir, "music", store=self.store, origin="local")
        self.run_music(miroir)
        ingested = self.status(miroir)["last_ingestion_success"]
        plans = []
        operation = request_models(miroir, "music", store=self.store, origin="local")
        outcome = execute_operation(
            miroir,
            operation.id,
            store=self.store,
            destination="unused",
            resolve_credentials=lambda c: self.fail(
                "model rebuild fetched source credentials"
            ),
            transform=plans.append,
        )
        self.assertEqual(
            (outcome.ingestion, outcome.transformation), ("not_run", "succeeded")
        )
        self.assertEqual(outcome.load_ids, ())
        self.assertEqual(
            plans[0].variables["miroir_sources"],
            {"listenbrainz": miroir.connections[0].binding.dataset_name},
        )
        status = self.status(miroir)
        self.assertEqual(self.analysis(miroir)["state"], "current")
        self.assertEqual(status["last_ingestion_success"], ingested)
        self.assertEqual(self.analysis(miroir)["built_receipt"], plans[0].receipt)

        def failing(plan):
            raise RuntimeError("model test failed")

        operation = request_models(miroir, "music", store=self.store, origin="local")
        outcome = execute_operation(
            miroir,
            operation.id,
            store=self.store,
            destination="unused",
            resolve_credentials=lambda c: self.fail("source credentials requested"),
            transform=failing,
        )
        self.assertEqual(
            (outcome.ingestion, outcome.transformation), ("not_run", "failed")
        )
        # dbt may have replaced some relations before a model/test failed.
        self.assertEqual(self.analysis(miroir)["state"], "stale")
        self.assertEqual(self.status(miroir)["last_ingestion_success"], ingested)
        operation = request_models(miroir, "music", store=self.store, origin="local")
        execute_operation(
            miroir,
            operation.id,
            store=self.store,
            destination="unused",
            resolve_credentials=lambda c: self.fail("source credentials requested"),
            transform=plans.append,
        )
        self.assertEqual(self.analysis(miroir)["state"], "current")

        requested = request_models(miroir, "music", store=self.store, origin="local")
        config = example()
        config["connections"] = config["connections"][:1]
        config["analyses"] = [
            {
                "id": "music",
                "package": "music",
                "inputs": {"music": "music"},
                "timezone": "America/Toronto",
            }
        ]
        changed = parse_miroir(json.dumps(config))
        self.assertEqual(
            changed.connections[0].revision, miroir.connections[0].revision
        )
        with self.assertRaisesRegex(
            OperationError, "Analysis settings or models changed"
        ):
            execute_operation(
                changed,
                requested.id,
                store=self.store,
                destination="unused",
                resolve_credentials=lambda c: self.fail("source credentials requested"),
                transform=plans.append,
            )

    def test_crashed_model_build_preserves_successful_ingestion(self):
        miroir = self.miroir()
        self.run_music(miroir, transform=lambda plan: None)
        (build,) = [
            o
            for o in self.store.recent_operations(miroir.id, "music")
            if o["kind"] == "models"
        ]
        self.assertEqual(build["analysis_id"], "music")
        with self.store.engine.connect() as db:
            attempt = db.execute(
                sa.select(attempt_table.c.id).where(
                    attempt_table.c.operation_id == build["id"]
                )
            ).scalar_one()
        self.store.update_attempt(attempt, transformation="running")
        self.store.close_unverified(build["id"], attempt, "lost", "Worker crashed")
        status = self.status(miroir)
        self.assertEqual(self.analysis(miroir)["last_outcome"], "lost")
        self.assertIsNotNone(status["last_ingestion_success"])
        self.assertEqual(self.analysis(miroir)["state"], "stale")
        self.store.update_attempt(attempt, transformation="succeeded")
        self.assertFalse(
            self.store.close_unverified(
                build["id"], attempt, "lost", "Late observation"
            )
        )
        self.assertEqual(self.analysis(miroir)["state"], "current")

    def test_ingestion_failure_is_recorded_and_redacted(self):
        miroir = self.miroir()
        with self.assertRaises(IngestionFailed) as raised:
            self.run_music(miroir, send=leaking_failure)
        status = self.status(miroir)
        self.assertEqual(status["last_attempt"]["ingestion"], "failed")
        self.assertEqual(status["last_attempt"]["transformation"], "not_run")
        self.assertEqual(self.analysis(miroir)["state"], "no_data")
        self.assertNotIn(TOKEN, json.dumps(status) + str(raised.exception))
        self.assertIn("***", status["last_attempt"]["error_message"])

    def test_backend_retry_is_a_new_attempt_of_the_same_operation(self):
        miroir = self.miroir()
        with self.assertRaises(IngestionFailed):
            self.run_music(
                miroir,
                send=leaking_failure,
                origin="prefect",
                backend_run_id="run-1",
            )
        outcome = self.run_music(miroir, origin="prefect", backend_run_id="run-1")
        self.assertEqual(outcome.attempt, 2)
        attempts = self.store.attempts(outcome.operation_id)
        self.assertEqual([a["ingestion"] for a in attempts], ["failed", "succeeded"])
        self.assertEqual(self.rows(miroir.connections[0].binding.dataset_name), 1)

    def test_requested_operation_is_adopted_by_its_backend_run(self):
        miroir = self.miroir(second_account=True)
        requested = self.store.create_operation(
            miroir_id=miroir.id,
            connection_id="music",
            integration="listenbrainz",
            kind="sync",
            origin="request",
            pipeline_name="p",
            dataset_name="d",
            config_revision=miroir.connections[0].revision,
            code_revision="test",
            backend_run_id=None,
        )
        self.assertEqual(self.status(miroir)["requested_not_started"], 1)
        with self.assertRaises(OperationError):
            self.run_music(
                miroir, connection_id="other_music", operation_id=requested.id
            )
        outcome = self.run_music(
            miroir,
            origin="request",
            operation_id=requested.id,
            backend_run_id="run-a",
        )
        self.assertEqual(outcome.operation_id, requested.id)
        self.assertEqual(self.status(miroir)["requested_not_started"], 0)
        with self.assertRaises(OperationError):
            self.run_music(miroir, operation_id=requested.id, backend_run_id="run-b")

    def test_same_connection_is_serialized_and_others_are_not(self):
        miroir = self.miroir(second_account=True)
        pipeline = miroir.connections[0].binding.pipeline_name
        with self.store.connection_lock(pipeline):
            with self.assertRaises(ConnectionBusy):
                self.run_music(miroir)
            self.run_music(miroir, connection_id="other_music")
        self.assertEqual(self.status(miroir)["last_attempt"]["ingestion"], "blocked")
        self.assertEqual(self.run_music(miroir).ingestion, "succeeded")

    def test_waiting_run_starts_when_the_running_one_finishes(self):
        import threading

        miroir = self.miroir()
        pipeline = miroir.connections[0].binding.pipeline_name
        held, release = threading.Event(), threading.Event()

        def hold():
            with self.store.connection_lock(pipeline):
                held.set()
                release.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        held.wait(10)
        threading.Timer(1.5, release.set).start()
        with patch("src.core.operations.LOCK_POLL_SECONDS", 0.1):
            outcome = self.run_music(miroir, lock_wait=10)
        holder.join()
        self.assertEqual(outcome.ingestion, "succeeded")
        self.assertEqual(outcome.attempt, 1)  # no blocked attempt first

    def test_metadata_outage_after_load_does_not_fail_the_load(self):
        miroir = self.miroir()
        store = self.open_store("meta.sqlite")
        original = store.update_attempt

        def flaky(attempt, **values):
            if values.get("ingestion") == "succeeded":
                raise OperationalError("UPDATE", {}, Exception("database is gone"))
            original(attempt, **values)

        store.update_attempt = flaky
        with self.assertLogs("src.core.runner", "ERROR"):
            outcome = self.run_music(miroir, store=store)
        self.assertFalse(outcome.recorded)
        self.assertEqual(outcome.ingestion, "succeeded")

    def test_nothing_loads_without_registration(self):
        miroir = self.miroir()
        store = self.open_store("unmigrated.sqlite")
        with self.assertRaises(sa.exc.OperationalError):
            self.run_music(miroir, store=store)
        self.assertFalse(self.database.exists())

    def test_records_survive_restart_and_schema_version_is_checked(self):
        miroir = self.miroir()
        outcome = self.run_music(miroir)
        reopened = self.open_store("meta.sqlite")
        reopened.migrate()
        self.assertEqual(
            reopened.get_operation(outcome.operation_id).connection_id, "music"
        )
        with reopened.engine.begin() as db:
            db.execute(schema_info.update().values(version=99))
        with self.assertRaises(OperationError):
            reopened.migrate()

    def test_version_3_upgrade_adds_delivery_tokens_and_preserves_history(self):
        from src.core.runner import request_sync

        operation = request_sync(
            self.miroir(), "music", store=self.store, origin="request"
        )
        artifact, _ = self.store.register_artifact(
            miroir_id="personal",
            connection_id="health",
            sha256="a" * 64,
            size=1,
            input_format="health_auto_export_json",
            options='{"role": "ongoing"}',
            original_name="export.json",
            storage_path="/unused/export.json",
            retention="keep",
        )
        with self.store.engine.begin() as db:
            delivery_tokens.drop(db)
            db.execute(sa.text("ALTER TABLE artifacts DROP COLUMN last_received_at"))
            db.execute(schema_info.update().values(version=3))
        self.store.migrate()
        self.store.migrate()
        self.assertEqual(self.store.get_operation(operation.id).connection_id, "music")
        self.assertEqual(
            self.store.get_artifact("personal", artifact["id"])["last_received_at"],
            artifact["received_at"],
        )
        self.store.set_delivery_token("personal", "health", "a" * 64)
        self.store.set_delivery_token("personal", "health", "b" * 64)
        self.assertEqual(
            self.store.delivery_token("personal", "health")["token_hash"], "b" * 64
        )
        self.assertIsNone(self.store.delivery_token("other", "health"))
        with self.store.engine.connect() as db:
            self.assertEqual(
                db.execute(sa.select(schema_info.c.version)).scalar(), SCHEMA_VERSION
            )


class SecretTests(unittest.TestCase):
    def test_resolves_only_declared_references(self):
        connection = parse_miroir(json.dumps(example())).connections[0]
        calls = []

        def provider(key):
            calls.append(key)
            return "value"

        self.assertEqual(
            resolve_credentials(connection, {"fnox": provider, "env": provider}),
            {"access_token": "value"},
        )
        self.assertEqual(calls, ["LISTENBRAINZ__ACCESS_TOKEN"])
        with self.assertRaises(SecretResolutionError) as raised:
            resolve_credentials(connection, {"fnox": lambda key: None})
        self.assertNotIn("LISTENBRAINZ__ACCESS_TOKEN", str(raised.exception))


class MetadataUrlTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            name: value
            for name, value in os.environ.items()
            if name == "MIROIR_METADATA_URL" or name.startswith("MIROIR_METADATA__")
        }
        for name in self.env:
            os.environ.pop(name)
        self.addCleanup(
            lambda: [
                os.environ.__setitem__(name, value) for name, value in self.env.items()
            ]
        )

    def test_a_direct_url_wins(self):
        os.environ["MIROIR_METADATA_URL"] = "sqlite:///direct.sqlite"
        os.environ["MIROIR_METADATA__HOST"] = "h"
        self.assertEqual(metadata_url(), "sqlite:///direct.sqlite")

    def test_composes_from_individual_parts(self):
        parts = {
            "MIROIR_METADATA__HOST": "postgres-rw.automation.svc",
            "MIROIR_METADATA__USERNAME": "metabase",
            "MIROIR_METADATA__PASSWORD": "p@ss word/2",
        }
        os.environ.update(parts)
        # Port and database default to the CNPG convention: 5432, db = username.
        self.assertEqual(
            metadata_url(),
            "postgresql+psycopg2://metabase:p%40ss+word%2F2@postgres-rw.automation.svc:5432/metabase",
        )
        os.environ.update(
            {
                "MIROIR_METADATA__PORT": "5433",
                "MIROIR_METADATA__DATABASE": "other",
            }
        )
        self.assertEqual(
            metadata_url(),
            "postgresql+psycopg2://metabase:p%40ss+word%2F2@postgres-rw.automation.svc:5433/other",
        )

    def test_unconfigured_is_none(self):
        os.environ["MIROIR_METADATA__HOST"] = "h"
        self.assertIsNone(metadata_url())  # username and password missing


class CliOperationTests(unittest.TestCase):
    def test_local_file_import_then_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "health").mkdir()
            (root / "health" / "export.json").write_text(
                json.dumps(
                    {
                        "data": {
                            "metrics": [
                                {
                                    "name": "steps",
                                    "units": "count",
                                    "data": [
                                        {"date": "2026-07-01 12:00:00 -0400", "qty": 5}
                                    ],
                                }
                            ]
                        }
                    }
                )
            )
            config = example()
            config["connections"][1]["settings"]["data_dir"] = str(root / "health")
            path = root / "miroir.json"
            path.write_text(json.dumps(config))
            common = ["--config", str(path)]
            metadata = ["--metadata", f"sqlite:///{root / 'meta.sqlite'}"]
            with redirect_stdout(io.StringIO()) as out:
                code = main(
                    [
                        *common,
                        "run",
                        "health",
                        "--duckdb",
                        str(root / "w.duckdb"),
                        *metadata,
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())["ingestion"], "succeeded")
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main([*common, "status", *metadata]), 0)
            status = {s["connection"]: s for s in json.loads(out.getvalue())}
            self.assertEqual(status["health"]["last_attempt"]["origin"], "local")
            self.assertIsNone(status["music"]["last_attempt"])


if __name__ == "__main__":
    unittest.main()
