"""Development launch selection, credential reuse and infrastructure prerequisites."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.dev import main
from src.core.secrets import SecretBox, generate_key


class DevLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        previous = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, previous)

    def launch(self, *args, env=None):
        output = io.StringIO()
        with (
            patch.dict(os.environ, env or {}, clear=True),
            patch("scripts.dev.os.execve") as execute,
            redirect_stdout(output),
            redirect_stderr(output),
        ):
            result = main(list(args))
        return result, execute, output.getvalue()

    def test_local_ignores_inherited_installation_and_reuses_private_credentials(self):
        installation = {"schema_version": 1, "id": "real", "connections": []}
        Path("miroir.json").write_text(json.dumps(installation))
        result, execute, output = self.launch(
            "local",
            "--port",
            "9876",
            env={
                "MIROIR_METADATA_URL": "postgresql://real:real-password@real/real",
                "MIROIR_METADATA__PASSWORD": "real-password",
                "MIROIR_SECRET_KEY": "real-key",
                "MIROIR_API_TOKEN": "real-token",
                "MIROIR_PREFECT_POOL": "real-pool",
                "PREFECT_API_URL": "https://real/api",
                "DESTINATION__POSTGRES__CREDENTIALS__PASSWORD": "real-password",
            },
        )
        self.assertEqual(result, 0, output)
        _, command, env = execute.call_args.args
        self.assertIn("sqlite:///.miroir/dev/metadata.sqlite", command)
        self.assertIn(".miroir/dev/warehouse.duckdb", command)
        self.assertEqual(command[-2:], ["--port", "9876"])
        for key in (
            "MIROIR_METADATA_URL",
            "MIROIR_METADATA__PASSWORD",
            "MIROIR_PREFECT_POOL",
            "PREFECT_API_URL",
            "DESTINATION__POSTGRES__CREDENTIALS__PASSWORD",
        ):
            self.assertNotIn(key, env)
        box = SecretBox.from_keys(env["MIROIR_SECRET_KEY"])
        encrypted = box.encrypt("local credential")
        _, again, _ = self.launch("local")
        again_env = again.call_args.args[2]
        self.assertEqual(env["MIROIR_API_TOKEN"], again_env["MIROIR_API_TOKEN"])
        self.assertEqual(
            SecretBox.from_keys(again_env["MIROIR_SECRET_KEY"]).decrypt(encrypted),
            "local credential",
        )
        for name in ("api-token", "secret-key"):
            self.assertEqual((Path(".miroir/dev") / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(".miroir/dev").stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(Path("miroir.json").read_text()), installation)
        self.assertNotIn(env["MIROIR_API_TOKEN"], output)
        self.assertNotIn(env["MIROIR_SECRET_KEY"], output)

    def test_local_refuses_a_deployment_in_its_own_configuration(self):
        Path(".miroir/dev").mkdir(parents=True)
        Path(".miroir/dev/miroir.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": "dev",
                    "connections": [],
                    "deployment": {"work_pool": "real-pool"},
                }
            )
        )
        result, execute, output = self.launch("local")
        self.assertEqual(result, 2)
        execute.assert_not_called()
        self.assertIn("cannot have a deployment", output)

    def infrastructure(self):
        config = Path("installation.json")
        config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": "installation",
                    "connections": [],
                    "deployment": {"work_pool": "installation-pool"},
                }
            )
        )
        Path("shared-artifacts").mkdir()
        return {
            "MIROIR_METADATA__HOST": "database.internal",
            "MIROIR_METADATA__USERNAME": "miroir",
            "MIROIR_METADATA__PASSWORD": "database-password",
            "MIROIR_SECRET_KEY": generate_key(),
            "PREFECT_API_URL": "http://prefect.internal/api",
            "PREFECT_API_AUTH_STRING": "installation-auth",
            "MIROIR_API_TOKEN": "deployed-server-token",
        }

    def test_infra_uses_granted_credentials_without_putting_them_in_arguments(self):
        inputs = self.infrastructure()
        result, execute, output = self.launch(
            "infra",
            "--config",
            "installation.json",
            "--artifacts",
            "shared-artifacts",
            "--host",
            "0.0.0.0",
            "--port",
            "12345",
            env=inputs,
        )
        self.assertEqual(result, 0, output)
        _, command, env = execute.call_args.args
        self.assertNotIn("--duckdb", command)
        self.assertNotIn("--metadata", command)
        self.assertEqual(command[-4:], ["--host", "0.0.0.0", "--port", "12345"])
        self.assertEqual(env["MIROIR_SECRET_KEY"], inputs["MIROIR_SECRET_KEY"])
        self.assertIn("database.internal", env["MIROIR_METADATA_URL"])
        self.assertEqual(
            env["MIROIR_PREFECT_CONFIG_PATH"], "/miroir/data/miroir/miroir.json"
        )
        self.assertNotEqual(env["MIROIR_API_TOKEN"], inputs["MIROIR_API_TOKEN"])
        for key in (
            "MIROIR_SECRET_KEY",
            "MIROIR_METADATA__PASSWORD",
            "PREFECT_API_AUTH_STRING",
            "MIROIR_API_TOKEN",
        ):
            self.assertNotIn(inputs[key], " ".join(command) + output)
        self.assertFalse(Path(".miroir/dev").exists())

    def test_infra_never_falls_back_for_missing_credentials_config_or_artifacts(self):
        inputs = self.infrastructure()
        for change, expected in (
            ({"MIROIR_SECRET_KEY": ""}, "Missing infrastructure credentials"),
            ({"MIROIR_METADATA_URL": "sqlite:///local.sqlite"}, "requires PostgreSQL"),
        ):
            with self.subTest(change=change):
                result, execute, output = self.launch("infra", env=inputs | change)
                self.assertEqual(result, 2)
                execute.assert_not_called()
                self.assertIn(expected, output)
        result, execute, output = self.launch("infra", env=inputs)
        self.assertEqual(result, 2)
        execute.assert_not_called()
        self.assertIn("config is missing", output)
        result, execute, output = self.launch(
            "infra", "--config", "installation.json", env=inputs
        )
        self.assertEqual(result, 2)
        execute.assert_not_called()
        self.assertIn("artifacts are missing", output)
        self.assertFalse(Path(".miroir").exists())

    def test_infra_requires_a_configured_prefect_deployment(self):
        inputs = self.infrastructure()
        Path("installation.json").write_text(
            '{"schema_version":1,"id":"new","connections":[]}'
        )
        result, execute, output = self.launch(
            "infra", "--config", "installation.json", env=inputs
        )
        self.assertEqual(result, 2)
        execute.assert_not_called()
        self.assertIn("must define deployment.work_pool", output)
