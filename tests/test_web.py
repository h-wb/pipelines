"""HTTP API and web interface over the shared services.

Executions run in-process (InlineExecutor) against temporary DuckDB and SQLite
files, except one test that drives the real subprocess executor. HTTP calls to
ListenBrainz are intercepted.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dlt
import requests
from fastapi.testclient import TestClient

from src.core.config import load_mirroir
from src.core.operations import MetadataStore, OperationError
from src.core.runner import execute_operation
from src.core.service import InlineExecutor, MirroirService, SubprocessExecutor
from src.web.app import SESSION_COOKIE, create_app
from tests.test_artifacts import export
from tests.test_mirroir import example
from tests.test_operations import listens_response

TOKEN = "test-token-0123456789"
BEARER = {"Authorization": f"Bearer {TOKEN}"}


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        config = example()
        config["links"] = {
            "prefect_ui": "https://prefect.example",
            "metabase": "https://metabase.example/",
        }
        config["connections"][1]["dashboards"] = {"Health": 24}
        self.config_path = self.root / "mirroir.json"
        self.config_path.write_text(json.dumps(config, indent=2))
        self.store = MetadataStore(f"sqlite:///{self.root / 'meta.sqlite'}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.executed = []
        self.service = MirroirService(
            self.config_path,
            self.store,
            self.root / "artifacts",
            InlineExecutor(self.execute),
        )
        retries = patch.dict(os.environ, {"RUNTIME__REQUEST_MAX_ATTEMPTS": "1"})
        retries.start()
        self.addCleanup(retries.stop)
        http = patch.object(requests.Session, "send", listens_response)
        http.start()
        self.addCleanup(http.stop)
        self.client = TestClient(create_app(self.service, TOKEN, max_upload_bytes=4096))

    def execute(self, operation_id):
        self.executed.append(operation_id)
        return execute_operation(
            load_mirroir(self.config_path),
            operation_id,
            store=self.store,
            destination=dlt.destinations.duckdb(
                credentials=str(self.root / "w.duckdb")
            ),
            resolve_credentials=lambda c: (
                {"access_token": "fake"} if c.config.credentials else {}
            ),
            pipelines_dir=str(self.root / "state"),
        )

    def login(self):
        response = self.client.post(
            "/login", data={"token": TOKEN}, follow_redirects=False
        )
        self.assertEqual(response.status_code, 303)
        return response

    def test_everything_but_login_requires_the_token(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/api/overview").status_code, 401)
        bad = {"Authorization": "Bearer wrong-token-0000000"}
        self.assertEqual(self.client.get("/api/overview", headers=bad).status_code, 401)
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(
            (response.status_code, response.headers["location"]), (303, "/login")
        )
        self.assertEqual(
            self.client.post("/login", data={"token": "nope"}).status_code, 401
        )
        self.assertEqual(
            self.client.get("/api/overview", headers=BEARER).status_code, 200
        )

    def test_session_cookie_is_not_the_token_and_blocks_cross_origin_posts(self):
        cookie = self.login().cookies[SESSION_COOKIE]
        self.assertNotIn(TOKEN, cookie)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("music", page.text)
        self.assertIn("https://metabase.example/dashboard/24", page.text)
        response = self.client.post(
            "/connections/music/sync",
            headers={"Origin": "https://evil.example"},
            follow_redirects=False,
        )
        self.assertEqual(response.headers["location"], "/login")
        self.assertEqual(self.executed, [])

    def test_phone_push_stores_and_imports_once(self):
        body = json.dumps(export(("2026-06-05", 1)))
        url = (
            "/api/connections/health/artifacts?role=ongoing&import=true&name=auto.json"
        )
        first = self.client.post(url, content=body, headers=BEARER)
        self.assertEqual(first.status_code, 201)
        again = self.client.post(url, content=body, headers=BEARER)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["already_imported"], first.json()["operation_id"])
        detail = self.client.get("/api/connections/health", headers=BEARER).json()
        self.assertEqual(detail["status"]["last_attempt"]["ingestion"], "succeeded")
        self.assertEqual(detail["artifacts"][0]["original_name"], "auto.json")
        # Generated dataset: dbt never reads it, which is not the same as stale.
        self.assertEqual(detail["status"]["analysis"], "not_modelled")
        self.assertEqual(detail["operations"][0]["artifact_name"], "auto.json")
        self.assertEqual(len(self.executed), 1)

    def test_bad_uploads_are_rejected_and_not_kept(self):
        url = "/api/connections/health/artifacts?role=ongoing"
        self.assertEqual(
            self.client.post(url, content=b"{}", headers=BEARER).status_code, 400
        )
        self.assertEqual(
            self.client.post(url, content=b"x" * 5000, headers=BEARER).status_code, 413
        )
        bad_role = "/api/connections/health/artifacts?role=other"
        body = json.dumps(export(("2026-06-05", 1)))
        self.assertEqual(
            self.client.post(bad_role, content=body, headers=BEARER).status_code, 400
        )
        no_files = "/api/connections/music/artifacts?role=ongoing"
        self.assertEqual(
            self.client.post(no_files, content=body, headers=BEARER).status_code, 400
        )
        stored = [p for p in (self.root / "artifacts").rglob("*") if p.is_file()]
        self.assertEqual(stored, [])

    def test_form_upload_escapes_file_names_and_offers_replay(self):
        self.login()
        response = self.client.post(
            "/connections/health/upload",
            files={
                "file": (
                    "<script>x</script>.json",
                    json.dumps(export(("2026-06-05", 1))),
                )
            },
            data={"role": "ongoing", "import_now": "true"},
            follow_redirects=False,
        )
        self.assertEqual(
            response.headers["location"], "/connections/health?notice=import_requested"
        )
        page = self.client.get(response.headers["location"])
        self.assertNotIn("<script>x</script>", page.text)
        self.assertIn("&lt;script&gt;", page.text)
        self.assertIn("Import requested.", page.text)
        artifact = self.service.artifacts("health")[0]["id"]
        replay = self.client.post(
            f"/connections/health/artifacts/{artifact}/import",
            data={"replay": "true"},
            follow_redirects=False,
        )
        self.assertIn("import_requested", replay.headers["location"])
        again = self.client.post(
            f"/connections/health/artifacts/{artifact}/import", follow_redirects=False
        )
        self.assertIn("already_imported", again.headers["location"])

    def test_sync_then_saved_vs_applied_and_queued_requests_are_blocked(self):
        self.login()
        self.client.post("/connections/music/sync")
        music = self.client.get("/api/connections/music", headers=BEARER).json()
        self.assertEqual(music["status"]["last_attempt"]["ingestion"], "succeeded")
        self.assertEqual(music["configuration"], "applied")

        # Queue a request without running it, then edit that connection.
        self.service.executor = type("Hold", (), {"submit": lambda self, op: None})()
        queued = self.service.request_sync("music")
        config = json.loads(self.config_path.read_text())
        config["connections"][0]["settings"]["start_date"] = "2021-01-01T00:00:00Z"
        current = self.client.get("/api/config", headers=BEARER).json()
        saved = self.client.put(
            "/api/config",
            content=json.dumps(config),
            headers={**BEARER, "If-Match": current["file_revision"]},
        )
        self.assertEqual(saved.status_code, 200)
        music = self.client.get("/api/connections/music", headers=BEARER).json()
        self.assertEqual(music["configuration"], "saved_not_applied")
        health = self.client.get("/api/connections/health", headers=BEARER).json()
        self.assertEqual(health["configuration"], "never_run")
        with self.assertRaises(OperationError):
            self.execute(queued)
        self.assertEqual(self.store.attempts(queued)[0]["ingestion"], "blocked")

    def test_config_edits_detect_conflicts_and_reject_invalid_files(self):
        original = self.config_path.read_text()
        current = self.client.get("/api/config", headers=BEARER).json()
        stale = {**BEARER, "If-Match": "0" * 64}
        self.assertEqual(
            self.client.put("/api/config", content=original, headers=stale).status_code,
            409,
        )
        fresh = {**BEARER, "If-Match": current["file_revision"]}
        self.assertEqual(
            self.client.put("/api/config", content="{", headers=fresh).status_code, 400
        )
        self.assertEqual(self.config_path.read_text(), original)
        self.assertEqual(
            self.client.put(
                "/api/config", content=original, headers=BEARER
            ).status_code,
            400,
        )

        self.login()
        edited = original.replace('"example_user"', '"renamed_user"')
        self.config_path.write_text(original + "\n")  # someone else saved meanwhile
        response = self.client.post(
            "/config", data={"text": edited, "file_revision": current["file_revision"]}
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("changed since you loaded it", response.text)
        self.assertEqual(self.config_path.read_text(), original + "\n")

    def test_prefect_run_links_use_the_backend_run(self):
        from src.core.runner import run_connection

        mirroir = load_mirroir(self.config_path)
        run_connection(
            mirroir,
            "music",
            store=self.store,
            origin="prefect",
            backend_run_id="abc-123",
            destination=dlt.destinations.duckdb(
                credentials=str(self.root / "w.duckdb")
            ),
            resolve_credentials=lambda c: {"access_token": "fake"},
            pipelines_dir=str(self.root / "state"),
        )
        last = self.client.get("/api/overview", headers=BEARER).json()["connections"][0]
        self.assertEqual(
            last["status"]["last_attempt"]["run_url"],
            "https://prefect.example/runs/flow-run/abc-123",
        )


class SubprocessExecutorTests(unittest.TestCase):
    def test_server_executor_runs_the_cli_entry_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = example()
            (root / "health").mkdir()
            (root / "health" / "e.json").write_text(
                json.dumps(export(("2026-06-05", 1)))
            )
            config["connections"][1]["settings"]["data_dir"] = str(root / "health")
            path = root / "mirroir.json"
            path.write_text(json.dumps(config))
            metadata = f"sqlite:///{root / 'meta.sqlite'}"
            store = MetadataStore(metadata)
            self.addCleanup(store.engine.dispose)
            store.migrate()
            executor = SubprocessExecutor(
                [
                    "--config",
                    str(path),
                    "execute",
                    "--duckdb",
                    str(root / "w.duckdb"),
                    "--metadata",
                    metadata,
                ]
            )
            service = MirroirService(path, store, root / "artifacts", executor)
            operation = service.request_sync("health")
            self.assertEqual(executor.children[0].wait(timeout=120), 0)
            attempts = store.attempts(operation)
            self.assertEqual([a["ingestion"] for a in attempts], ["succeeded"])


if __name__ == "__main__":
    unittest.main()
