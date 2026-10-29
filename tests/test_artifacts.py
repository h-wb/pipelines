"""Explicit artifact import for Apple Health, against synthetic exports.

Covers the milestone 3 cases: invalid, duplicate, and overlapping files, changed
contents with unchanged timestamps, failed and interrupted imports, parser version
changes, the crash window after a load commits, and the v1 -> v2 schema upgrade.
"""

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import dlt
import duckdb
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

from src.cli.main import main
from src.core.artifacts import receive_file, submit_artifact
from src.core.config import parse_miroir
from src.core.operations import (
    SCHEMA_VERSION,
    MetadataStore,
    OperationError,
    schema_info,
)
from src.core.runner import AlreadyImported, IngestionFailed, import_artifact
from src.integrations.registry import INTEGRATIONS
from src.integrations.validation import InvalidArtifact
from src.sources.apple_health import apple_health as legacy_health_source
from tests.test_miroir import example

CUTOFF = "2026-06-04"  # from examples/miroir.json


def export(*points):
    """A Health Auto Export document with one step-count point per (date, qty)."""
    data = [{"date": f"{day} 12:00:00 -0400", "qty": qty} for day, qty in points]
    return {"data": {"metrics": [{"name": "steps", "units": "count", "data": data}]}}


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.open_store()
        self.store.migrate()
        self.database = self.root / "warehouse.duckdb"
        config = example()
        config["connections"] = config["connections"][1:]
        other = copy.deepcopy(config["connections"][0])
        other["id"] = "other_health"
        config["connections"].append(other)
        self.config = config
        self.miroir = parse_miroir(json.dumps(config))
        self.health = self.miroir.connections[0]

    def open_store(self):
        store = MetadataStore(f"sqlite:///{self.root / 'meta.sqlite'}")
        self.addCleanup(store.engine.dispose)
        return store

    def write(self, name, document):
        path = self.root / "inbox" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            json.dumps(document) if not isinstance(document, str) else document
        )
        return path

    def submit(self, path, role="ongoing", connection=None, store=None):
        return submit_artifact(
            self.miroir,
            connection or self.health,
            path,
            {"role": role},
            store=store or self.store,
            root=self.root / "artifacts",
        )

    def load(self, artifact, store=None, **kwargs):
        return import_artifact(
            self.miroir,
            artifact["connection_id"],
            artifact["id"],
            store=store or self.store,
            destination=dlt.destinations.duckdb(credentials=str(self.database)),
            resolve_credentials=lambda connection: {},
            origin="local",
            pipelines_dir=str(self.root / "state"),
            **kwargs,
        )

    def metrics(self, connection=None):
        dataset = (connection or self.health).binding.dataset_name
        with duckdb.connect(str(self.database)) as db:
            return db.execute(
                f'SELECT CAST(date AS VARCHAR)[:10], qty FROM "{dataset}".metrics ORDER BY 1'
            ).fetchall()

    def test_role_selects_rows_by_cutoff_without_path_conventions(self):
        both_sides = export(("2026-06-03", 1), ("2026-06-05", 2))
        historical, _ = self.submit(self.write("bulk.json", both_sides), "historical")
        self.load(historical)
        self.assertEqual([qty for _, qty in self.metrics()], [1])
        ongoing_file = export(("2026-06-03", 1), ("2026-06-05", 2), ("2026-06-06", 3))
        ongoing, _ = self.submit(self.write("daily.json", ongoing_file), "ongoing")
        self.load(ongoing)
        self.assertEqual([qty for _, qty in self.metrics()], [1, 2, 3])

    def test_invalid_files_are_rejected_before_storage(self):
        for content in (
            "not json",
            {"data": []},
            {"data": {}},
            {"data": {"metrics": [1]}},
        ):
            with self.subTest(content=content), self.assertRaises(InvalidArtifact):
                self.submit(self.write("bad.json", content))
        self.assertFalse((self.root / "artifacts").exists())
        self.assertEqual(self.store.list_artifacts(self.miroir.id, "health"), [])

    def test_identity_is_per_connection_and_role_conflicts_are_refused(self):
        path = self.write("a.json", export(("2026-06-05", 1)))
        first, created = self.submit(path)
        again, created_again = self.submit(path)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["id"], again["id"])
        with self.assertRaises(OperationError):
            self.submit(path, role="historical")
        other, _ = self.submit(path, connection=self.miroir.connections[1])
        self.assertNotEqual(other["id"], first["id"])
        self.assertEqual(oct(os.stat(first["storage_path"]).st_mode)[-3:], "440")

    def test_repeat_is_skipped_replay_is_merge_safe_overlap_merges(self):
        artifact, _ = self.submit(self.write("a.json", export(("2026-06-05", 1))))
        first = self.load(artifact)
        with self.assertRaises(AlreadyImported) as raised:
            self.load(artifact)
        self.assertEqual(raised.exception.operation_id, first.operation_id)
        replay = self.load(artifact, replay=True)
        self.assertEqual(self.store.get_operation(replay.operation_id).kind, "replay")
        self.assertEqual(len(self.metrics()), 1)
        overlap = export(("2026-06-05", 1), ("2026-06-06", 2))
        self.load(self.submit(self.write("b.json", overlap))[0])
        self.assertEqual([qty for _, qty in self.metrics()], [1, 2])

    def test_changed_contents_with_unchanged_mtime_are_imported(self):
        directory = self.root / "health"
        directory.mkdir()
        legacy = directory / "export.json"
        legacy.write_text(json.dumps(export(("2026-06-05", 1))))
        stamp = os.stat(legacy).st_mtime
        connection = parse_miroir(
            json.dumps(
                {
                    **example(),
                    "connections": [
                        {
                            **example()["connections"][1],
                            "settings": {"data_dir": str(directory), "cutoff": CUTOFF},
                        }
                    ],
                }
            )
        ).connections[0]
        pipeline = dlt.pipeline(
            pipeline_name=connection.binding.pipeline_name,
            pipelines_dir=str(self.root / "state"),
            destination=dlt.destinations.duckdb(credentials=str(self.database)),
            dataset_name=connection.binding.dataset_name,
        )
        pipeline.run(legacy_health_source(data_dir=str(directory), cutoff=CUTOFF))
        legacy.write_text(json.dumps(export(("2026-06-05", 1), ("2026-06-06", 2))))
        os.utime(legacy, (stamp, stamp))
        pipeline.run(legacy_health_source(data_dir=str(directory), cutoff=CUTOFF))
        # The legacy scanner's modification-time cursor skips the changed file...
        self.assertEqual(len(self.metrics()), 1)
        # ...while the same bytes submitted explicitly are a new artifact and load.
        self.load(self.submit(legacy)[0])
        self.assertEqual(len(self.metrics()), 2)

    def test_failed_import_is_not_imported_and_can_be_retried(self):
        broken = {"data": {"metrics": [{"units": "count", "data": [{"date": "x"}]}]}}
        artifact, _ = self.submit(self.write("broken.json", broken))
        for _ in range(2):  # not AlreadyImported the second time
            with self.assertRaises(IngestionFailed):
                self.load(artifact)
        imports = self.store.artifact_imports(artifact["id"])
        self.assertEqual([i["ingestion"] for i in imports], ["failed", "failed"])

    def test_new_parser_version_reimports(self):
        artifact, _ = self.submit(self.write("a.json", export(("2026-06-05", 1))))
        self.load(artifact)
        health = self.health.integration
        newer = replace(health, formats=(replace(health.formats[0], version=2),))
        registry = {**INTEGRATIONS, "apple_health": newer}
        with patch("src.core.config.INTEGRATIONS", registry):
            self.miroir = parse_miroir(json.dumps(self.config))
        outcome = self.load(artifact)
        self.assertEqual(
            self.store.get_operation(outcome.operation_id).parser_version, 2
        )

    def test_crash_window_after_commit_reimports_safely(self):
        artifact, _ = self.submit(self.write("a.json", export(("2026-06-05", 1))))
        store = self.open_store()
        original = store.update_attempt

        def lost_completion(attempt, **values):
            if values.get("ingestion") == "succeeded":
                raise OperationalError("UPDATE", {}, Exception("connection lost"))
            original(attempt, **values)

        store.update_attempt = lost_completion
        with self.assertLogs("src.core.runner", "ERROR"):
            self.assertFalse(self.load(artifact, store=store).recorded)
        # Not recorded as imported, so a plain import runs again without duplicates.
        self.assertEqual(self.load(artifact).ingestion, "succeeded")
        self.assertEqual(len(self.metrics()), 1)

    def test_tampered_storage_is_refused(self):
        artifact, _ = self.submit(self.write("a.json", export(("2026-06-05", 1))))
        stored = Path(artifact["storage_path"])
        os.chmod(stored, 0o640)
        stored.write_text(json.dumps(export(("2026-06-05", 99))))
        with self.assertRaises(OperationError):
            self.load(artifact)

    def test_interrupted_copy_leaves_nothing(self):
        class Failing:
            name = "upload.json"

            def open(self, mode):
                reader = io.BytesIO(b"x" * 10)
                reader.read = lambda size=-1: (_ for _ in ()).throw(OSError("cut"))
                return reader

        with self.assertRaises(OSError):
            receive_file(Failing(), self.root / "artifacts", "personal", "health")
        self.assertEqual(list((self.root / "artifacts").rglob("*.*")), [])
        self.assertEqual(
            [p for p in (self.root / "artifacts").rglob("*") if p.is_file()], []
        )

    def test_version_1_store_upgrades_to_current(self):
        path = self.root / "v1.sqlite"
        legacy = MetadataStore(f"sqlite:///{path}")
        self.addCleanup(legacy.engine.dispose)
        legacy.migrate()
        with legacy.engine.begin() as db:
            for table in ("artifacts", "secrets", "connection_checks"):
                db.execute(sa.text(f"DROP TABLE {table}"))
            for column in (
                "artifact_id",
                "parser_version",
                "options",
                "parent_operation_id",
            ):
                db.execute(sa.text(f"ALTER TABLE operations DROP COLUMN {column}"))
            db.execute(sa.text("ALTER TABLE attempts DROP COLUMN failure_kind"))
            db.execute(schema_info.update().values(version=1))
            db.execute(
                sa.text(
                    "INSERT INTO operations VALUES ('op', 'personal', 'health', "
                    "'apple_health', 'sync', 'local', 'p', 'd', 'r', 'c', NULL, "
                    "'2026-10-28 00:00:00')"
                )
            )
        legacy.migrate()
        self.assertEqual(legacy.get_operation("op").kind, "sync")
        with legacy.engine.connect() as db:
            self.assertEqual(
                db.execute(sa.select(schema_info.c.version)).scalar(), SCHEMA_VERSION
            )
            inspector = sa.inspect(db)
            columns = {c["name"] for c in inspector.get_columns("operations")}
            attempt_columns = {c["name"] for c in inspector.get_columns("attempts")}
            tables = set(inspector.get_table_names())
        self.assertTrue(
            {"artifact_id", "parser_version", "options", "parent_operation_id"}
            <= columns
        )
        self.assertIn("failure_kind", attempt_columns)
        self.assertTrue({"artifacts", "secrets", "connection_checks"} <= tables)

    def test_version_4_roles_become_options_and_receipts_still_match(self):
        artifact, _ = self.submit(
            self.write("a.json", export(("2026-06-03", 1))), "historical"
        )
        self.load(artifact)
        with self.store.engine.begin() as db:
            for column in ("options", "origin", "removed_at"):
                db.execute(sa.text(f"ALTER TABLE artifacts DROP COLUMN {column}"))
            db.execute(sa.text("ALTER TABLE artifacts ADD COLUMN role VARCHAR(16)"))
            db.execute(sa.text("UPDATE artifacts SET role = 'historical'"))
            db.execute(
                sa.text("ALTER TABLE operations DROP COLUMN parent_operation_id")
            )
            db.execute(schema_info.update().values(version=4))
        self.store.migrate()
        upgraded = self.store.get_artifact(self.miroir.id, artifact["id"])
        self.assertEqual(upgraded["options"], {"role": "historical"})
        self.assertIsNone(upgraded["removed_at"])
        # Identical bytes keep their recorded options; the import is not repeated.
        self.assertEqual(
            self.submit(Path(artifact["storage_path"]), "historical")[0]["id"],
            artifact["id"],
        )
        with self.assertRaises(AlreadyImported):
            self.load(upgraded)


class ArtifactCliTests(unittest.TestCase):
    def test_submit_import_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "miroir.json"
            path.write_text(json.dumps(example()))
            exported = root / "export.json"
            exported.write_text(json.dumps(export(("2026-06-05", 1))))
            base = ["--config", str(path)]
            metadata = ["--metadata", f"sqlite:///{root / 'meta.sqlite'}"]

            def run(*args):
                with redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(main([*base, *args, *metadata]), 0)
                return json.loads(out.getvalue())

            submitted = run(
                "submit",
                "health",
                str(exported),
                "--option",
                "role=ongoing",
                "--artifacts",
                str(root / "artifacts"),
            )
            artifact = submitted["artifact"]["id"]
            duckdb_path = str(root / "w.duckdb")
            self.assertEqual(
                run("import", "health", artifact, "--duckdb", duckdb_path)["ingestion"],
                "succeeded",
            )
            self.assertTrue(
                run("import", "health", artifact, "--duckdb", duckdb_path)[
                    "already_imported"
                ]
            )
            listed = run("artifacts", "health")
            self.assertEqual(listed[0]["original_name"], "export.json")
            self.assertEqual(
                [i["ingestion"] for i in listed[0]["imports"]], ["succeeded"]
            )
            self.assertEqual(
                listed[0]["imports"][0]["options"],
                {"cutoff": CUTOFF, "role": "ongoing"},
            )


if __name__ == "__main__":
    unittest.main()
