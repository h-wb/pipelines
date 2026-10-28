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
from src.core.config import parse_workspace
from src.core.operations import (
    ConnectionBusy,
    MetadataStore,
    OperationError,
    schema_info,
)
from src.core.runner import IngestionFailed, run_connection
from src.core.secrets import SecretResolutionError, resolve_credentials
from tests.test_workspace import example

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

    def workspace(self, legacy_binding=False, second_account=False):
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
        return parse_workspace(json.dumps(config))

    def run_music(self, workspace, send=listens_response, store=None, **kwargs):
        kwargs.setdefault("origin", "local")
        with patch.object(requests.Session, "send", send):
            return run_connection(
                workspace,
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

    def status(self, workspace, connection="music"):
        return self.store.connection_status(workspace.id, connection)

    def test_sync_records_load_and_skips_models_for_generated_dataset(self):
        workspace = self.workspace()
        outcome = self.run_music(workspace)
        self.assertEqual(
            (outcome.ingestion, outcome.transformation), ("succeeded", "skipped")
        )
        self.assertTrue(outcome.load_ids)
        operation = self.store.get_operation(outcome.operation_id)
        self.assertEqual(operation.config_revision, workspace.revision)
        self.assertEqual(
            operation.dataset_name, workspace.connections[0].binding.dataset_name
        )
        status = self.status(workspace)
        self.assertEqual(status["last_attempt"]["load_ids"], list(outcome.load_ids))
        self.assertIn(
            "models read listenbrainz_data",
            status["last_attempt"]["transformation_detail"],
        )
        # Loaded but never modelled: analysis does not reflect this data.
        self.assertEqual(status["analysis"], "stale")

    def test_model_failure_keeps_load_and_marks_analysis_stale(self):
        workspace = self.workspace(legacy_binding=True)
        selectors = []
        self.run_music(workspace, transform=selectors.append)
        self.assertEqual(selectors, ["source:listenbrainz+"])
        self.assertEqual(self.status(workspace)["analysis"], "current")

        def failing(selector):
            raise RuntimeError(f"dbt failed near {TOKEN}")

        outcome = self.run_music(workspace, transform=failing)
        self.assertEqual(
            (outcome.ingestion, outcome.transformation), ("succeeded", "failed")
        )
        status = self.status(workspace)
        self.assertEqual(status["analysis"], "stale")
        self.assertNotIn(TOKEN, json.dumps(status))
        self.assertEqual(self.rows("listenbrainz_data"), 1)

    def test_ingestion_failure_is_recorded_and_redacted(self):
        workspace = self.workspace()
        with self.assertRaises(IngestionFailed) as raised:
            self.run_music(workspace, send=leaking_failure)
        status = self.status(workspace)
        self.assertEqual(status["last_attempt"]["ingestion"], "failed")
        self.assertEqual(status["last_attempt"]["transformation"], "not_run")
        self.assertEqual(status["analysis"], "no_data")
        self.assertNotIn(TOKEN, json.dumps(status) + str(raised.exception))
        self.assertIn("***", status["last_attempt"]["error_message"])

    def test_backend_retry_is_a_new_attempt_of_the_same_operation(self):
        workspace = self.workspace()
        with self.assertRaises(IngestionFailed):
            self.run_music(
                workspace,
                send=leaking_failure,
                origin="prefect",
                backend_run_id="run-1",
            )
        outcome = self.run_music(workspace, origin="prefect", backend_run_id="run-1")
        self.assertEqual(outcome.attempt, 2)
        attempts = self.store.attempts(outcome.operation_id)
        self.assertEqual([a["ingestion"] for a in attempts], ["failed", "succeeded"])
        self.assertEqual(self.rows(workspace.connections[0].binding.dataset_name), 1)

    def test_requested_operation_is_adopted_by_its_backend_run(self):
        workspace = self.workspace(second_account=True)
        requested = self.store.create_operation(
            workspace_id=workspace.id,
            connection_id="music",
            integration="listenbrainz",
            kind="sync",
            origin="request",
            pipeline_name="p",
            dataset_name="d",
            config_revision=workspace.revision,
            code_revision="test",
            backend_run_id=None,
        )
        self.assertEqual(self.status(workspace)["requested_not_started"], 1)
        with self.assertRaises(OperationError):
            self.run_music(
                workspace, connection_id="other_music", operation_id=requested.id
            )
        outcome = self.run_music(
            workspace,
            origin="request",
            operation_id=requested.id,
            backend_run_id="run-a",
        )
        self.assertEqual(outcome.operation_id, requested.id)
        self.assertEqual(self.status(workspace)["requested_not_started"], 0)
        with self.assertRaises(OperationError):
            self.run_music(workspace, operation_id=requested.id, backend_run_id="run-b")

    def test_same_connection_is_serialized_and_others_are_not(self):
        workspace = self.workspace(second_account=True)
        pipeline = workspace.connections[0].binding.pipeline_name
        with self.store.connection_lock(pipeline):
            with self.assertRaises(ConnectionBusy):
                self.run_music(workspace)
            self.run_music(workspace, connection_id="other_music")
        self.assertEqual(self.status(workspace)["last_attempt"]["ingestion"], "blocked")
        self.assertEqual(self.run_music(workspace).ingestion, "succeeded")

    def test_metadata_outage_after_load_does_not_fail_the_load(self):
        workspace = self.workspace()
        store = self.open_store("meta.sqlite")
        original = store.update_attempt

        def flaky(attempt, **values):
            if values.get("ingestion") == "succeeded":
                raise OperationalError("UPDATE", {}, Exception("database is gone"))
            original(attempt, **values)

        store.update_attempt = flaky
        with self.assertLogs("src.core.runner", "ERROR"):
            outcome = self.run_music(workspace, store=store)
        self.assertFalse(outcome.recorded)
        self.assertEqual(outcome.ingestion, "succeeded")

    def test_nothing_loads_without_registration(self):
        workspace = self.workspace()
        store = self.open_store("unmigrated.sqlite")
        with self.assertRaises(sa.exc.OperationalError):
            self.run_music(workspace, store=store)
        self.assertFalse(self.database.exists())

    def test_records_survive_restart_and_schema_version_is_checked(self):
        workspace = self.workspace()
        outcome = self.run_music(workspace)
        reopened = self.open_store("meta.sqlite")
        reopened.migrate()
        self.assertEqual(
            reopened.get_operation(outcome.operation_id).connection_id, "music"
        )
        with reopened.engine.begin() as db:
            db.execute(schema_info.update().values(version=99))
        with self.assertRaises(OperationError):
            reopened.migrate()


class SecretTests(unittest.TestCase):
    def test_resolves_only_declared_references(self):
        connection = parse_workspace(json.dumps(example())).connections[0]
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
            path = root / "workspace.json"
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
