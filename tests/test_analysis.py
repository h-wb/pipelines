"""Connection-bound dbt builds against a disposable real Postgres.

Set MIROIR_TEST_POSTGRES_BIN to an installed Postgres bin directory, or put
initdb/pg_ctl on PATH. No server, production credentials, or Prefect API is used.
"""

import getpass
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from contextlib import closing, nullcontext
from pathlib import Path
from unittest.mock import patch

import dlt
import psycopg2

from src.core.analysis import analysis_plan
from src.core.artifacts import submit_artifact
from src.core.config import ConfigurationError, parse_miroir
from src.core.operations import MetadataStore
from src.core.runner import (
    execute_operation,
    import_artifact,
    request_models,
    run_connection,
)
from src.core.service import InlineExecutor, MiroirService
from src.pipelines.transform import build_connection_models
from tests.test_miroir import example
from tests.test_operations import listens_response


class AnalysisPlanTests(unittest.TestCase):
    def test_bindings_accounts_and_timezone_are_part_of_the_plan(self):
        config = example()
        first = parse_miroir(json.dumps(config)).connections[0]
        plan = analysis_plan(first)
        self.assertEqual(
            plan.variables["miroir_sources"],
            {"listenbrainz": first.binding.dataset_name},
        )
        self.assertEqual(plan.selector, "+path:models/music")
        config["connections"][0]["binding"] = {
            "pipeline_name": "p",
            "dataset_name": "old_music",
        }
        config["connections"][0]["analysis"] = {"timezone": "America/Toronto"}
        bound = analysis_plan(parse_miroir(json.dumps(config)).connections[0])
        self.assertEqual(bound.relations, plan.relations)
        self.assertNotEqual(bound.receipt, plan.receipt)
        self.assertEqual(
            bound.variables["miroir_sources"], {"listenbrainz": "old_music"}
        )
        config["connections"][0]["id"] = "other_music"
        other = analysis_plan(parse_miroir(json.dumps(config)).connections[0])
        self.assertNotEqual(other.relations, bound.relations)
        config["connections"][0]["analysis"]["timezone"] = "not-a-timezone"
        with self.assertRaises(ConfigurationError):
            parse_miroir(json.dumps(config))

    def test_maximum_ids_fit_postgres_and_ambiguous_identity_pairs_do_not_alias(self):
        def plan(miroir_id, connection_id):
            config = example()
            config["id"] = miroir_id
            config["connections"][0]["id"] = connection_id
            return analysis_plan(parse_miroir(json.dumps(config)).connections[0])

        longest = plan("a" * 24, "b" * 24)
        self.assertLessEqual(
            len(longest.variables["miroir_namespace"] + "_intermediate"), 63
        )
        self.assertNotEqual(plan("a_b", "c").relations, plan("a", "b_c").relations)


class DbtPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = os.environ.get("MIROIR_TEST_POSTGRES_BIN")
        cls.initdb = str(Path(binary) / "initdb") if binary else shutil.which("initdb")
        cls.pg_ctl = str(Path(binary) / "pg_ctl") if binary else shutil.which("pg_ctl")
        if not cls.initdb or not cls.pg_ctl:
            raise unittest.SkipTest(
                "Postgres binaries unavailable; set MIROIR_TEST_POSTGRES_BIN"
            )
        cls.tmp = tempfile.TemporaryDirectory(prefix="miroir-models-")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        cls.socket = cls.root / "socket"
        cls.socket.mkdir()
        cls.datadir = cls.root / "postgres"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        subprocess.run(
            [
                cls.initdb,
                "-D",
                str(cls.datadir),
                "-A",
                "trust",
                "--no-locale",
                "--encoding=UTF8",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                cls.pg_ctl,
                "-D",
                str(cls.datadir),
                "-l",
                str(cls.root / "postgres.log"),
                "-o",
                f"-F -k {cls.socket} -h 127.0.0.1 -p {cls.port}",
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
        )
        cls.addClassCleanup(
            subprocess.run,
            [cls.pg_ctl, "-D", str(cls.datadir), "-m", "fast", "-w", "stop"],
            check=True,
            capture_output=True,
        )
        cls.env = {
            "DESTINATION__POSTGRES__CREDENTIALS__HOST": "127.0.0.1",
            "DESTINATION__POSTGRES__CREDENTIALS__PORT": str(cls.port),
            "DESTINATION__POSTGRES__CREDENTIALS__USERNAME": getpass.getuser(),
            "DESTINATION__POSTGRES__CREDENTIALS__PASSWORD": "",
            "DESTINATION__POSTGRES__CREDENTIALS__DATABASE": "postgres",
        }
        environment = patch.dict(os.environ, cls.env)
        environment.start()
        cls.addClassCleanup(environment.stop)
        concurrency = patch(
            "src.pipelines.transform.concurrency",
            side_effect=lambda *a, **kw: nullcontext(),
        )
        concurrency.start()
        cls.addClassCleanup(concurrency.stop)

    def setUp(self):
        self.store = MetadataStore(
            f"postgresql+psycopg2://{getpass.getuser()}@127.0.0.1:{self.port}/postgres"
        )
        self.store.migrate()
        self.addCleanup(self.store.engine.dispose)
        self.destination = dlt.destinations.postgres(
            credentials={
                "host": "127.0.0.1",
                "port": self.port,
                "database": "postgres",
                "username": getpass.getuser(),
                "password": "",
            }
        )
        self.execution = {
            "store": self.store,
            "destination": self.destination,
            "resolve_credentials": lambda c: (
                {"access_token": "fake"} if c.config.credentials else {}
            ),
            "pipelines_dir": str(self.root / self._testMethodName / "dlt"),
            "transform": build_connection_models,
        }

    def query(self, sql):
        with (
            closing(
                psycopg2.connect(
                    host="127.0.0.1",
                    port=self.port,
                    user=getpass.getuser(),
                    dbname="postgres",
                )
            ) as db,
            db,
            db.cursor() as cursor,
        ):
            cursor.execute(sql)
            return cursor.fetchall() if cursor.description else []

    def configuration(self, connections):
        return parse_miroir(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": "analysis_tests",
                    "connections": connections,
                }
            )
        )

    def health(self, connection_id):
        return {
            "id": connection_id,
            "integration": "apple_health",
            "settings": {"cutoff": "2020-01-01"},
        }

    def load_export(self, miroir, connection_id, body):
        path = self.root / "export.json"
        path.write_text(json.dumps({"data": body}))
        connection = next(c for c in miroir.connections if c.config.id == connection_id)
        artifact, _ = submit_artifact(
            miroir,
            connection,
            path,
            "ongoing",
            store=self.store,
            root=self.root / "artifacts",
        )
        return import_artifact(
            miroir, connection_id, artifact["id"], origin="local", **self.execution
        )

    def test_metrics_only_accounts_sparse_schema_and_evolving_exports(self):
        first = self.health("health_one")
        first["binding"] = {
            "pipeline_name": "custom_health",
            "dataset_name": "custom_health_raw",
        }
        miroir = self.configuration([first, self.health("health_two")])

        def metrics(qty):
            return {
                "metrics": [
                    {
                        "name": "step_count",
                        "units": "count",
                        "data": [{"date": "2026-10-29 12:00:00 -0400", "qty": qty}],
                    }
                ]
            }

        result = self.load_export(miroir, "health_one", metrics(12))
        self.assertEqual(result.transformation, "succeeded")
        one, two = [analysis_plan(c) for c in miroir.connections]
        self.assertEqual(
            self.query(f"select steps, sleep_h from {one.relations[0]}"), [(12, None)]
        )
        self.assertEqual(self.query(f"select count(*) from {one.relations[1]}"), [(0,)])
        result = self.load_export(miroir, "health_two", metrics(999))
        self.assertEqual(result.transformation, "succeeded")
        self.assertEqual(self.query(f"select steps from {two.relations[0]}"), [(999,)])
        self.assertEqual(self.query(f"select steps from {one.relations[0]}"), [(12,)])
        evolved = metrics(13.5)
        evolved["metrics"].append(
            {
                "name": "sleep_analysis",
                "units": "hr",
                "data": [
                    {
                        "date": "2026-10-29 08:00:00 -0400",
                        "totalSleep": 7.5,
                        "deep": 1.25,
                    }
                ],
            }
        )
        evolved["workouts"] = [
            {
                "id": "workout-one",
                "name": "Walk",
                "start": "2026-10-29 12:00:00 -0400",
                "duration": 1200,
                "distance": {"qty": 2},
            }
        ]
        result = self.load_export(miroir, "health_one", evolved)
        self.assertEqual(result.transformation, "succeeded")
        self.assertEqual(
            self.query(f"select steps, sleep_h, deep_h from {one.relations[0]}"),
            [(13.5, 7.5, 1.25)],
        )
        self.assertEqual(
            self.query(
                f"select workout_id, duration_min, avg_hr from {one.relations[1]}"
            ),
            [("workout-one", 20, None)],
        )
        schemas = {
            row[0]
            for row in self.query("select schema_name from information_schema.schemata")
        }
        self.assertTrue(
            {
                "health",
                "music",
                "staging",
                "crossovers",
                "bikeshare_data",
                "listenbrainz_data",
            }.isdisjoint(schemas)
        )
        self.assertFalse(any(s.endswith("_crossovers") for s in schemas))

    def test_optional_resources_are_empty_required_fields_fail_and_legacy_tables_survive(
        self,
    ):
        miroir = self.configuration([self.health("empty_health")])
        plan = analysis_plan(miroir.connections[0])
        self.query(
            "create schema health; create table health.daily_summary (sentinel int); insert into health.daily_summary values (42)"
        )
        try:
            build_connection_models(plan)
            self.assertEqual(
                self.query(f"select count(*) from {plan.relations[0]}"), [(0,)]
            )
            self.assertEqual(
                self.query("select sentinel from health.daily_summary"), [(42,)]
            )
            raw_schema = miroir.connections[0].binding.dataset_name
            self.query(
                f"create schema {raw_schema}; create table {raw_schema}.metrics (metric_name text)"
            )
            with self.assertRaisesRegex(RuntimeError, "dbt build failed"):
                build_connection_models(plan)
            self.assertEqual(
                self.query("select sentinel from health.daily_summary"), [(42,)]
            )
        finally:
            self.query("drop schema health cascade")

    def test_sparse_music_is_independent_and_rebuild_changes_timezone_without_loading(
        self,
    ):
        music = example()["connections"][0]
        music["id"] = "music_models"
        miroir = self.configuration([music])
        with patch("requests.Session.send", listens_response):
            outcome = run_connection(
                miroir, "music_models", origin="local", **self.execution
            )
        self.assertEqual(outcome.transformation, "succeeded")
        plan = analysis_plan(miroir.connections[0])
        self.assertEqual(
            self.query(f"select count(*) from {plan.relations[1]}"), [(1,)]
        )
        self.assertEqual(
            self.query(f"select service from {plan.relations[1]}"), [("Other",)]
        )
        before = self.query(f"select day, hour from {plan.relations[1]}")[0]
        ingested = self.store.connection_status(miroir.id, "music_models")[
            "last_ingestion_success"
        ]
        music["analysis"] = {"timezone": "America/Toronto"}
        miroir = self.configuration([music])
        operation = request_models(
            miroir, "music_models", store=self.store, origin="local"
        )
        execution = {
            **self.execution,
            "resolve_credentials": lambda c: self.fail(
                "Rebuild resolved source credentials"
            ),
        }
        outcome = execute_operation(miroir, operation.id, **execution)
        self.assertEqual(
            (outcome.ingestion, outcome.transformation), ("not_run", "succeeded")
        )
        after = self.query(f"select day, hour from {plan.relations[1]}")[0]
        self.assertNotEqual(before, after)
        self.assertEqual(
            self.store.connection_status(miroir.id, "music_models")[
                "last_ingestion_success"
            ],
            ingested,
        )
        config_path = self.root / "miroir.json"
        config_path.write_text(
            json.dumps({"schema_version": 1, "id": miroir.id, "connections": [music]})
        )
        service = MiroirService(
            config_path,
            self.store,
            self.root / "artifacts",
            InlineExecutor(lambda _: None),
        )
        self.assertEqual(
            service.connection("music_models")["status"]["analysis"], "current"
        )
        music["analysis"]["timezone"] = "Europe/Paris"
        config_path.write_text(
            json.dumps({"schema_version": 1, "id": miroir.id, "connections": [music]})
        )
        self.assertEqual(
            service.connection("music_models")["status"]["analysis"], "stale"
        )
