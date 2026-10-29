"""Watched folders (decision 021): a transport that turns files into artifacts."""

import json
import os
import tempfile
import unittest
from pathlib import Path

import dlt
import duckdb

from src.core.config import ConfigurationError, parse_miroir
from src.core.operations import MetadataStore, OperationError
from src.core.runner import IngestionFailed, import_artifact, run_connection

CUTOFF = "2026-06-04"


def export(*days):
    return {
        "data": {
            "metrics": [
                {
                    "name": "step_count",
                    "units": "count",
                    "data": [
                        {"date": f"{day} 12:00:00 -0400", "qty": qty}
                        for day, qty in days
                    ],
                }
            ]
        }
    }


class FolderSyncTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.store = MetadataStore(f"sqlite:///{self.root / 'meta.sqlite'}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.builds = []

    def configure(self, **folder):
        connection = {
            "id": "health",
            "integration": "apple_health",
            "settings": {"cutoff": CUTOFF},
            "folder": {"path": str(self.inbox), **folder},
        }
        self.miroir = parse_miroir(
            json.dumps(
                {"schema_version": 1, "id": "tests", "connections": [connection]}
            )
        )
        return self.miroir.connections[0]

    def sync(self):
        return run_connection(self.miroir, "health", origin="local", **self.execution())

    def execution(self):
        return {
            "store": self.store,
            "destination": dlt.destinations.duckdb(
                credentials=str(self.root / "warehouse.duckdb")
            ),
            "resolve_credentials": lambda connection: {},
            "pipelines_dir": str(self.root / "state"),
            "artifact_root": self.root / "artifacts",
            "transform": self.builds.append,
        }

    def write(self, name, *days):
        (self.inbox / name).write_text(json.dumps(export(*days)))

    def metrics(self):
        dataset = self.miroir.connections[0].binding.dataset_name
        with duckdb.connect(str(self.root / "warehouse.duckdb")) as db:
            return db.execute(
                f'SELECT CAST(date AS VARCHAR)[:10], qty FROM "{dataset}".metrics '
                "ORDER BY 1"
            ).fetchall()

    def children(self, operation_id):
        return [
            o
            for o in self.store.recent_operations(self.miroir.id, "health", limit=100)
            if o["parent_operation_id"] == operation_id
        ]

    def test_new_files_are_imported_as_children_and_built_once(self):
        connection = self.configure(options={"role": "historical"})
        self.assertEqual(connection.config.folder.pattern, "*.json")
        self.write("a.json", ("2026-06-01", 1), ("2026-06-05", 9))
        self.write("b.json", ("2026-06-02", 2))
        (self.inbox / "notes.txt").write_text("ignored: does not match")
        outcome = self.sync()
        self.assertEqual(
            (outcome.ingestion, outcome.transformation), ("succeeded", "succeeded")
        )
        self.assertEqual(len(self.builds), 1)
        # Folder options apply to every file: historical keeps rows before the cutoff.
        self.assertEqual([qty for _, qty in self.metrics()], [1, 2])
        children = self.children(outcome.operation_id)
        self.assertEqual(sorted(c["kind"] for c in children), ["import", "import"])
        artifacts = self.store.list_artifacts(self.miroir.id, "health")
        self.assertEqual({a["origin"] for a in artifacts}, {"folder"})
        self.assertEqual({a["retention"] for a in artifacts}, {"rolling"})
        self.assertEqual(
            {json.dumps(a["options"]) for a in artifacts}, {'{"role": "historical"}'}
        )

        # Nothing new: no load, no build, and analysis is not made stale.
        again = self.sync()
        self.assertEqual(
            (again.ingestion, again.transformation), ("not_run", "not_run")
        )
        self.assertEqual(self.children(again.operation_id), [])
        self.assertEqual(len(self.builds), 1)
        status = self.store.connection_status(self.miroir.id, "health")
        self.assertNotEqual(status["analysis"], "stale")

        # A changed file is new content: only it is imported.
        self.write("b.json", ("2026-06-02", 2), ("2026-06-03", 3))
        changed = self.sync()
        self.assertEqual(len(self.children(changed.operation_id)), 1)
        self.assertEqual([qty for _, qty in self.metrics()], [1, 2, 3])
        self.assertEqual(len(self.builds), 2)

    def test_invalid_files_are_reported_and_valid_ones_still_load(self):
        self.configure()
        self.write("good.json", ("2026-06-05", 5))
        (self.inbox / "bad.json").write_text("not json")
        with self.assertRaisesRegex(IngestionFailed, "bad.json: not a valid file"):
            self.sync()
        self.assertEqual([qty for _, qty in self.metrics()], [5])
        self.assertEqual(len(self.builds), 1)
        # Invalid files are never stored.
        self.assertEqual(len(self.store.list_artifacts(self.miroir.id, "health")), 1)

    def test_retention_removes_old_copies_and_resending_restores_them(self):
        self.configure(keep_last=1)
        self.write("export.json", ("2026-06-05", 1))
        first = self.sync()
        self.write("export.json", ("2026-06-05", 1), ("2026-06-06", 2))
        self.sync()
        old, latest = sorted(
            self.store.list_artifacts(self.miroir.id, "health"),
            key=lambda a: a["received_at"],
        )
        self.assertIsNotNone(old["removed_at"])
        self.assertFalse(Path(old["storage_path"]).exists())
        self.assertIsNone(latest["removed_at"])
        self.assertTrue(Path(latest["storage_path"]).exists())
        # Receipts and history stay; replaying the removed copy is refused.
        self.assertEqual(len(self.children(first.operation_id)), 1)
        with self.assertRaisesRegex(OperationError, "Retention removed"):
            import_artifact(
                self.miroir,
                "health",
                old["id"],
                origin="local",
                replay=True,
                **self.execution(),
            )
        # The same bytes arriving again restore the stored copy.
        self.write("export.json", ("2026-06-05", 1))
        self.sync()
        restored = self.store.get_artifact(self.miroir.id, old["id"])
        self.assertIsNone(restored["removed_at"])
        self.assertTrue(Path(restored["storage_path"]).exists())

    def test_missing_folder_fails_the_sync_readably(self):
        self.configure()
        os.rmdir(self.inbox)
        with self.assertRaisesRegex(IngestionFailed, "watched folder does not exist"):
            self.sync()


class FolderConfigurationTests(unittest.TestCase):
    def parse(self, connection):
        return parse_miroir(
            json.dumps(
                {"schema_version": 1, "id": "tests", "connections": [connection]}
            )
        ).connections[0]

    def test_legacy_data_dir_becomes_a_watched_folder(self):
        connection = self.parse(
            {
                "id": "health",
                "integration": "apple_health",
                "settings": {"cutoff": CUTOFF, "data_dir": "/data/health"},
            }
        )
        self.assertEqual(connection.config.folder.path, "/data/health")
        self.assertEqual(connection.config.folder.format, "health_auto_export_json")
        self.assertEqual(connection.config.folder.options, {"role": "ongoing"})
        self.assertTrue(connection.can_sync)
        # A null data_dir (folder-free setup) means no folder.
        connection = self.parse(
            {
                "id": "health",
                "integration": "apple_health",
                "settings": {"cutoff": CUTOFF, "data_dir": None},
            }
        )
        self.assertIsNone(connection.config.folder)
        self.assertFalse(connection.can_sync)

    def test_folders_are_validated_against_the_integration(self):
        base = {
            "id": "health",
            "integration": "apple_health",
            "settings": {"cutoff": CUTOFF},
        }
        for folder in (
            {"path": "relative/path"},
            {"path": "/data", "pattern": "../*.json"},
            {"path": "/data", "format": "firefox_places_snapshot"},
            {"path": "/data", "options": {"role": "sometimes"}},
            {"path": "/data", "keep_last": 0},
        ):
            with self.subTest(folder=folder), self.assertRaises(ConfigurationError):
                self.parse({**base, "folder": folder})
        with self.assertRaisesRegex(ConfigurationError, "reads no files"):
            self.parse(
                {
                    "id": "music",
                    "integration": "listenbrainz",
                    "settings": {"username": "u", "start_date": "2020-01-01T00:00:00Z"},
                    "credentials": {"access_token": {"provider": "env", "key": "X"}},
                    "folder": {"path": "/data"},
                }
            )

    def test_retention_does_not_change_what_a_sync_loads(self):
        connection = {
            "id": "browsing",
            "integration": "browser_history",
            "settings": {},
            "folder": {"path": "/data/browser"},
        }
        first = self.parse(connection)
        self.assertEqual(first.config.folder.pattern, "*.sqlite.gz")
        connection["folder"]["keep_last"] = 10
        self.assertEqual(self.parse(connection).revision, first.revision)
        connection["folder"]["path"] = "/elsewhere"
        self.assertNotEqual(self.parse(connection).revision, first.revision)


if __name__ == "__main__":
    unittest.main()
