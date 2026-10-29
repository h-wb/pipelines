"""Connection secrets (decision 011): encryption, resolution, lifecycle, health.

Tokens are fake; ListenBrainz is intercepted at the requests boundary.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import dlt
import requests
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from src.cli.main import main
from src.core.config import ConfigurationConflict, ConfigurationError, parse_miroir
from src.core.operations import MetadataStore, OperationError
from src.core.runner import IngestionFailed, run_connection
from src.core.secrets import (
    SecretBox,
    SecretKeyError,
    SecretResolutionError,
    SecretStore,
    make_resolver,
)
from src.core.service import InlineExecutor, MiroirService
from src.web.app import create_app
from tests.test_operations import listens_response

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "lb-token-SENSITIVE-0001"
API = "test-token-0123456789"
BEARER = {"Authorization": f"Bearer {API}"}


def validate_token(user="me", status=200, valid=True):
    def get(url, headers=None, timeout=None):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps({"valid": valid, "user_name": user}).encode()
        return response

    return patch("requests.get", get)


def unauthorized(session, request, **kwargs):
    response = requests.Response()
    response.status_code = 401
    response.request = request
    response.url = request.url
    response._content = b'{"error": "Invalid token"}'
    return response


class SecretBoxTests(unittest.TestCase):
    def test_roundtrip_wrong_key_and_rotation(self):
        old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        token = SecretBox.from_keys(old).encrypt(TOKEN)
        self.assertNotIn(TOKEN.encode(), token)
        self.assertEqual(SecretBox.from_keys(old).decrypt(token), TOKEN)
        with self.assertRaises(SecretKeyError):
            SecretBox.from_keys(new).decrypt(token)
        rotated = SecretBox.from_keys(f"{new},{old}").rotate(token)
        self.assertEqual(SecretBox.from_keys(new).decrypt(rotated), TOKEN)
        for bad in ("", "not-a-key"):
            with self.assertRaises(SecretKeyError):
                SecretBox.from_keys(bad)

    def test_references_must_match_their_provider(self):
        base = json.loads((ROOT / "examples/miroir.json").read_text())
        for reference in ({"provider": "miroir", "key": "X"}, {"provider": "env"}):
            config = json.loads(json.dumps(base))
            config["connections"][0]["credentials"]["access_token"] = reference
            with self.assertRaises(ConfigurationError):
                parse_miroir(json.dumps(config))


class ConnectionSecretTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "miroir.json"
        self.config.write_text(
            json.dumps({"schema_version": 1, "id": "home", "connections": []})
        )
        self.store = MetadataStore(f"sqlite:///{self.root / 'meta.sqlite'}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.key = Fernet.generate_key().decode()
        self.secrets = SecretStore(self.store, SecretBox.from_keys(self.key))
        self.service = MiroirService(
            self.config,
            self.store,
            self.root / "artifacts",
            InlineExecutor(print),
            self.secrets,
        )
        retries = patch.dict(os.environ, {"RUNTIME__REQUEST_MAX_ATTEMPTS": "1"})
        retries.start()
        self.addCleanup(retries.stop)

    def add(self, connection_id="music", user="me", token=TOKEN):
        with validate_token(user="me"):
            return self.service.create_connection(
                "listenbrainz",
                connection_id,
                {"username": user, "start_date": "2020-01-01T00:00:00Z"},
                {"access_token": token},
            )

    def everything_on_disk(self):
        return b"".join(p.read_bytes() for p in self.root.rglob("*") if p.is_file())

    def test_created_connection_stores_only_a_reference_and_ciphertext(self):
        self.add()
        saved = json.loads(self.config.read_text())["connections"][0]
        self.assertEqual(saved["credentials"], {"access_token": {"provider": "miroir"}})
        self.assertNotIn(TOKEN.encode(), self.everything_on_disk())
        miroir = self.service.miroir()
        self.assertEqual(
            make_resolver(self.secrets)(miroir.connections[0]),
            {"access_token": TOKEN},
        )
        view = json.dumps(self.service.connection("music"))
        self.assertNotIn(TOKEN, view)
        self.assertIn('"set": true', view)

    def test_secrets_are_scoped_to_their_connection(self):
        self.add("music")
        self.add("other", token="other-token-0002")
        connections = {c.config.id: c for c in self.service.miroir().connections}
        resolve = make_resolver(self.secrets)
        self.assertEqual(
            resolve(connections["other"])["access_token"], "other-token-0002"
        )
        self.service.remove_connection("music")
        self.assertEqual(self.store.secret_metadata("home", "music"), {})
        self.assertEqual(
            resolve(connections["other"])["access_token"], "other-token-0002"
        )

    def test_failed_save_removes_stored_secrets_and_duplicates_keep_theirs(self):
        with (
            patch(
                "src.core.service.save_miroir",
                side_effect=ConfigurationConflict("x"),
            ),
            self.assertRaises(ConfigurationConflict),
        ):
            self.add()
        self.assertEqual(self.store.secret_metadata("home", "music"), {})
        self.add()
        with self.assertRaises(ConfigurationError):
            self.add()  # duplicate connection ID
        self.assertIn("access_token", self.store.secret_metadata("home", "music"))

    def test_invalid_settings_store_nothing(self):
        with self.assertRaises(ConfigurationError):
            self.service.create_connection(
                "listenbrainz",
                "music",
                {"username": "me", "start_date": "yesterday"},
                {"access_token": TOKEN},
            )
        self.assertEqual(self.store.secret_metadata("home", "music"), {})
        self.assertNotIn(TOKEN.encode(), self.everything_on_disk())

    def test_checks_and_runtime_rejection_drive_credential_status(self):
        self.add()
        with validate_token(user="me"):
            self.assertTrue(self.service.check_connection("music")["ok"])
        self.assertEqual(self.store.credential_status("home", "music")["state"], "ok")
        with validate_token(user="someone_else"):
            result = self.service.check_connection("music")
        self.assertEqual((result["ok"], result["rejected"]), (False, True))
        self.assertEqual(
            self.store.credential_status("home", "music")["state"], "rejected"
        )
        with validate_token(status=401):
            self.service.check_connection("music")
        with validate_token(user="me"):
            self.service.check_connection("music")
        # A run refused with 401 is newer evidence than the passing check.
        miroir = self.service.miroir()
        with (
            patch.object(requests.Session, "send", unauthorized),
            self.assertRaises(IngestionFailed),
        ):
            run_connection(
                miroir,
                "music",
                store=self.store,
                origin="local",
                destination=dlt.destinations.duckdb(
                    credentials=str(self.root / "w.duckdb")
                ),
                resolve_credentials=self.service.resolver(),
                pipelines_dir=str(self.root / "state"),
            )
        status = self.store.credential_status("home", "music")
        self.assertEqual((status["state"], status["source"]), ("rejected", "run"))
        self.service.set_credential("music", "access_token", "fresh-token-0003")
        with patch.object(requests.Session, "send", listens_response):
            run_connection(
                self.service.miroir(),
                "music",
                store=self.store,
                origin="local",
                destination=dlt.destinations.duckdb(
                    credentials=str(self.root / "w.duckdb")
                ),
                resolve_credentials=self.service.resolver(),
                pipelines_dir=str(self.root / "state"),
            )
        self.assertEqual(self.store.credential_status("home", "music")["state"], "ok")

    def test_missing_key_and_external_references(self):
        self.add()
        keyless = MiroirService(
            self.config, self.store, self.root, InlineExecutor(print)
        )
        with self.assertRaises(SecretResolutionError):
            keyless.resolver()(keyless.miroir().connections[0])
        with self.assertRaises(SecretResolutionError):
            keyless.set_credential("music", "access_token", "x")
        result = keyless.check_connection("music")
        self.assertFalse(result["ok"])
        # External references keep working and cannot be replaced from here.
        config = json.loads(self.config.read_text())
        config["connections"][0]["credentials"]["access_token"] = {
            "provider": "env",
            "key": "LB_TOKEN",
        }
        self.config.write_text(json.dumps(config))
        with patch.dict(os.environ, {"LB_TOKEN": "from-env"}):
            resolved = keyless.resolver()(keyless.miroir().connections[0])
        self.assertEqual(resolved, {"access_token": "from-env"})
        with self.assertRaises(OperationError):
            self.service.set_credential("music", "access_token", "x")

    def test_rotation_reencrypts_everything(self):
        self.add()
        new = Fernet.generate_key().decode()
        rotating = SecretStore(self.store, SecretBox.from_keys(f"{new},{self.key}"))
        self.assertEqual(rotating.rotate_all(), 1)
        only_new = SecretStore(self.store, SecretBox.from_keys(new))
        self.assertEqual(only_new.get("home", "music", "access_token"), TOKEN)


class SecretWebAndCliTests(ConnectionSecretTests):
    def client(self):
        return TestClient(create_app(self.service, API))

    def test_web_form_creates_connection_and_never_echoes_secrets(self):
        client = self.client()
        client.post("/login", data={"token": API})
        self.assertIn("ListenBrainz", client.get("/connections/new").text)
        form = client.get("/connections/new?integration=listenbrainz").text
        self.assertIn('type="password" name="credential.access_token"', form)
        bad = client.post(
            "/connections/new",
            data={
                "integration": "listenbrainz",
                "id": "music",
                "setting.username": "me",
                "setting.start_date": "yesterday",
                "credential.access_token": TOKEN,
            },
        )
        self.assertEqual(bad.status_code, 400)
        self.assertNotIn(TOKEN, bad.text)
        self.assertIn('value="me"', bad.text)
        with validate_token(user="me"):
            created = client.post(
                "/connections/new",
                data={
                    "integration": "listenbrainz",
                    "id": "music",
                    "setting.username": "me",
                    "setting.start_date": "2020-01-01T00:00:00Z",
                    "credential.access_token": TOKEN,
                },
                follow_redirects=False,
            )
        self.assertEqual(
            created.headers["location"], "/connections/music/setup?method=api"
        )
        page = client.get("/connections/music").text
        self.assertNotIn(TOKEN, page)
        self.assertNotIn('name="value"', page)
        page = client.get("/connections/music/settings").text
        self.assertNotIn(TOKEN, page)
        self.assertIn("Token valid for me", page)
        removed = client.post(
            "/connections/music/remove",
            data={"confirm": "music"},
            follow_redirects=False,
        )
        self.assertEqual(removed.headers["location"], "/?notice=removed")
        self.assertEqual(self.store.secret_metadata("home", "music"), {})

    def test_api_create_replace_check_and_remove(self):
        client = self.client()
        body = {
            "integration": "listenbrainz",
            "id": "music",
            "settings": {"username": "me", "start_date": "2020-01-01T00:00:00Z"},
            "credentials": {"access_token": TOKEN},
        }
        with validate_token(user="me"):
            created = client.post("/api/connections", json=body, headers=BEARER)
        self.assertEqual(created.status_code, 201)
        self.assertTrue(created.json()["check"]["ok"])
        replaced = client.put(
            "/api/connections/music/credentials/access_token",
            json={"value": "new-token-0004"},
            headers=BEARER,
        )
        self.assertEqual(replaced.status_code, 204)
        with validate_token(status=401):
            check = client.post("/api/connections/music/check", headers=BEARER).json()
        self.assertTrue(check["rejected"])
        everything = json.dumps(
            client.get("/api/connections/music", headers=BEARER).json()
        )
        self.assertNotIn("new-token-0004", everything)
        self.assertEqual(
            client.delete("/api/connections/music", headers=BEARER).status_code, 204
        )
        self.assertEqual(
            client.get("/api/overview", headers=BEARER).json()["connections"], []
        )

    def test_cli_set_credential_reads_stdin_and_check_exit_code(self):
        self.add()
        env = {
            **os.environ,
            "MIROIR_SECRET_KEY": self.key,
            "RUNTIME__DLTHUB_TELEMETRY": "false",
        }
        metadata = f"sqlite:///{self.root / 'meta.sqlite'}"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "src.cli",
                "--config",
                str(self.config),
                "set-credential",
                "music",
                "access_token",
                "--metadata",
                metadata,
            ],
            check=False,
            input="cli-token-0005\n",
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("cli-token-0005", result.stdout + result.stderr)
        self.assertEqual(
            self.secrets.get("home", "music", "access_token"), "cli-token-0005"
        )
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["generate-key"]), 0)
        Fernet(out.getvalue().strip().encode())  # a valid key


if __name__ == "__main__":
    unittest.main()
