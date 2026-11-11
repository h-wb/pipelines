"""Service behaviour when execution or checks fail outside the runner."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from sqlalchemy.engine import make_url

from src.core.artifacts import receive_file
from src.core.config import file_revision, save_miroir
from src.core.operations import OperationError
from src.core.runner import failure_kind
from tests.test_web import WebFixture


class Refusing:
    def __init__(self, error):
        self.error = error

    def submit(self, operation_id):
        raise self.error


class SubmitFailureTests(WebFixture):
    def test_a_refused_submission_closes_the_operation(self):
        self.service.executor = Refusing(OperationError("Prefect is down"))
        with self.assertRaisesRegex(OperationError, "Prefect is down"):
            self.service.request_sync("music")
        status = self.store.connection_status(self.service.miroir().id, "music")
        self.assertEqual(status["requested_not_started"], 0)
        self.assertEqual(
            (status["last_attempt"]["ingestion"], status["last_attempt"]["error_type"]),
            ("blocked", "SubmitFailed"),
        )
        # Unexpected executor errors are readable too.
        self.service.executor = Refusing(RuntimeError("socket closed"))
        with self.assertRaisesRegex(OperationError, "Could not start the run"):
            self.service.request_sync("music")

    def test_a_refused_rebuild_does_not_leave_the_analysis_running(self):
        self.service.request_sync("music")
        miroir_id = self.service.miroir().id
        self.assertEqual(self.service.analysis("music")["status"]["state"], "stale")
        self.service.executor = Refusing(OperationError("Prefect is down"))
        with self.assertRaises(OperationError):
            self.service.request_models("music")
        self.assertEqual(self.service.analysis("music")["status"]["state"], "stale")
        # A deployment that cannot be prepared records no rebuild at all.
        self.service.orchestrator = SimpleNamespace(
            plan=lambda miroir: (_ for _ in ()).throw(OperationError("no pool"))
        )
        before = self.store.recent_operations(miroir_id, "music", limit=100)
        with self.assertRaisesRegex(OperationError, "no pool"):
            self.service.request_models("music")
        self.assertEqual(
            self.store.recent_operations(miroir_id, "music", limit=100), before
        )


class LatestRequestTests(WebFixture):
    def test_a_backlog_of_child_imports_does_not_hide_the_sync(self):
        miroir = self.service.miroir()
        health = next(c for c in miroir.connections if c.config.id == "health")

        def record(**values):
            return self.store.create_operation(
                miroir_id=miroir.id,
                connection_id="health",
                integration=health.integration.name,
                origin="local",
                pipeline_name=health.binding.pipeline_name,
                dataset_name=health.binding.dataset_name,
                config_revision=health.revision,
                code_revision="test",
                **values,
            )

        sync = record(kind="sync")
        for _ in range(25):
            record(kind="import", parent_operation_id=sync.id)
        record(kind="models", analysis_id="health", parent_operation_id=sync.id)
        self.assertEqual(self.service.latest_request("health")["id"], sync.id)
        self.assertIsNone(self.service.latest_request("health", "webhook"))


class NeverStartedTests(WebFixture):
    def test_refresh_closes_old_requests_that_never_got_a_run(self):
        from datetime import datetime, timedelta, timezone

        miroir = self.service.miroir()
        music = next(c for c in miroir.connections if c.config.id == "music")
        old = datetime.now(timezone.utc) - timedelta(hours=2)

        def record(**values):
            return self.store.create_operation(
                miroir_id=miroir.id,
                connection_id="music",
                integration=music.integration.name,
                kind="sync",
                origin="request",
                pipeline_name=music.binding.pipeline_name,
                dataset_name=music.binding.dataset_name,
                config_revision=music.revision,
                code_revision="test",
                **values,
            )

        stuck = record(created_at=old)
        recent = record()
        running = record(created_at=old, backend_run_id="run-1")
        child = record(created_at=old, parent_operation_id=running.id)
        self.assertEqual(
            self.store.connection_status(miroir.id, "music")["requested_not_started"], 4
        )
        self.service.orchestrator = SimpleNamespace(observe=lambda miroir: [])
        closed = self.service.refresh()
        self.assertEqual([c["operation_id"] for c in closed], [stuck.id])
        attempt = self.store.attempts(stuck.id)[-1]
        self.assertEqual(
            (attempt["ingestion"], attempt["error_type"]), ("blocked", "NeverStarted")
        )
        for untouched in (recent, running, child):
            self.assertEqual(self.store.attempts(untouched.id), [])
        self.assertEqual(self.service.refresh(), [])


class CheckFailureTests(WebFixture):
    def test_unexpected_check_errors_are_recorded_and_redacted(self):
        def resolver():
            return lambda connection: {"access_token": "secret-token"}

        def explode(*args, **kwargs):
            raise RuntimeError("upstream echoed secret-token")

        with (
            patch.object(self.service, "resolver", resolver),
            patch.object(requests, "get", explode),
        ):
            result = self.service.check_connection("music")
        self.assertFalse(result["ok"])
        self.assertFalse(result["rejected"])
        self.assertIn("RuntimeError", result["message"])
        self.assertNotIn("secret-token", result["message"])


class SmallCoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_a_github_rate_limit_is_not_a_rejected_credential(self):
        def error(status, **headers):
            response = requests.Response()
            response.status_code = status
            response.headers.update(headers)
            return requests.HTTPError(response=response)

        self.assertEqual(failure_kind(error(403)), "credentials_rejected")
        self.assertIsNone(failure_kind(error(403, **{"X-RateLimit-Remaining": "0"})))
        self.assertIsNone(failure_kind(error(403, **{"Retry-After": "60"})))

    def test_identical_bytes_arriving_together_register_once(self):
        import sqlalchemy as sa

        from src.core.operations import MetadataStore

        store = MetadataStore(f"sqlite:///{self.root / 'meta.sqlite'}")
        self.addCleanup(store.engine.dispose)
        store.migrate()
        values = {
            "miroir_id": "m",
            "connection_id": "c",
            "sha256": "0" * 64,
            "size": 2,
            "input_format": "f",
            "options": "{}",
            "origin": "webhook",
            "original_name": "export.json",
            "storage_path": "/x",
            "retention": "keep",
        }
        original = MetadataStore._register_artifact
        raced = []

        def racing(self, values):
            if not raced:  # the other request inserts between our read and write
                raced.append(original(self, dict(values)))
                raise sa.exc.IntegrityError("INSERT", {}, Exception("duplicate"))
            return original(self, values)

        with patch.object(MetadataStore, "_register_artifact", racing):
            artifact, created = store.register_artifact(**values)
        self.assertEqual((artifact["id"], created), (raced[0][0]["id"], False))

    def test_a_secret_set_concurrently_ends_as_the_later_value(self):
        import sqlalchemy as sa

        from src.core.operations import MetadataStore

        store = MetadataStore(f"sqlite:///{self.root / 'secrets.sqlite'}")
        self.addCleanup(store.engine.dispose)
        store.migrate()
        original = MetadataStore._put_secret
        raced = []

        def racing(self, *args):
            if not raced:  # another request inserts first
                raced.append(original(self, "m", "c", "token", b"other"))
                raise sa.exc.IntegrityError("INSERT", {}, Exception("duplicate"))
            return original(self, *args)

        with patch.object(MetadataStore, "_put_secret", racing):
            store.put_secret("m", "c", "token", b"mine")
        self.assertEqual(store.get_secret("m", "c", "token"), b"mine")

    def test_a_damaged_stored_copy_is_replaced_by_the_same_bytes(self):
        source = self.root / "export.json"
        source.write_text("{}")
        stored = receive_file(source, self.root / "artifacts", "m", "c").path
        inode = stored.stat().st_ino
        receive_file(source, self.root / "artifacts", "m", "c")
        self.assertEqual(stored.stat().st_ino, inode)  # intact: left for its readers
        os.chmod(stored, 0o640)
        stored.write_text("damaged")
        again = receive_file(source, self.root / "artifacts", "m", "c")
        self.assertEqual(again.path.read_text(), "{}")
        self.assertEqual(stat.S_IMODE(again.path.stat().st_mode), 0o440)

    def test_saving_keeps_the_configuration_file_mode(self):
        path = self.root / "miroir.json"
        text = '{"schema_version": 1, "id": "tests", "connections": []}'
        path.write_text(text)
        os.chmod(path, 0o644)
        save_miroir(path, text + "\n", file_revision(path))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)


class MetadataPasswordTests(unittest.TestCase):
    def test_any_password_survives_the_url(self):
        password = "p@ss word/2+x%"
        env = {
            "MIROIR_METADATA__HOST": "h",
            "MIROIR_METADATA__USERNAME": "u",
            "MIROIR_METADATA__PASSWORD": password,
        }
        from src.core.config import metadata_url

        with patch.dict(os.environ, env, clear=True):
            url = metadata_url()
        assert url is not None
        self.assertEqual(make_url(url).password, password)


if __name__ == "__main__":
    unittest.main()
