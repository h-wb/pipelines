import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from src.cli.main import main
from src.core.config import ConfigurationError, parse_miroir

ROOT = Path(__file__).resolve().parents[1]


def example():
    return json.loads((ROOT / "examples/miroir.json").read_text())


class MiroirTests(unittest.TestCase):
    def parse(self, config):
        return parse_miroir(json.dumps(config))

    def test_revision_is_independent_of_format_key_and_connection_order(self):
        config = example()
        first = self.parse(config)
        config["connections"].reverse()
        second = parse_miroir(json.dumps(config, sort_keys=True, indent=4))
        self.assertEqual(first.revision, second.revision)
        config["connections"][0]["settings"]["cutoff"] = "2026-06-05"
        self.assertNotEqual(first.revision, self.parse(config).revision)

    def test_two_accounts_have_distinct_state_and_dataset_bindings(self):
        config = example()
        other = copy.deepcopy(config["connections"][0])
        other["id"] = "second_music"
        other["settings"]["username"] = "another_user"
        config["connections"].append(other)
        first, _, second = self.parse(config).connections
        self.assertNotEqual(first.binding.pipeline_name, second.binding.pipeline_name)
        self.assertNotEqual(first.binding.dataset_name, second.binding.dataset_name)

    def test_miroir_and_connection_underscores_do_not_alias(self):
        config = example()
        config["connections"] = config["connections"][:1]
        config["id"], config["connections"][0]["id"] = "a_b", "c"
        first = self.parse(config).connections[0].binding
        config["id"], config["connections"][0]["id"] = "a", "b_c"
        self.assertNotEqual(first, self.parse(config).connections[0].binding)

    def test_existing_bindings_can_be_preserved_explicitly(self):
        config = example()
        binding = {"pipeline_name": "listenbrainz", "dataset_name": "listenbrainz_data"}
        config["connections"][0]["binding"] = binding
        self.assertEqual(
            self.parse(config).connections[0].binding.model_dump(), binding
        )

    def test_rejects_each_kind_of_collision(self):
        for field in ("id", "pipeline_name", "dataset_name"):
            with self.subTest(field=field):
                config = example()
                first, second = config["connections"]
                if field == "id":
                    second["id"] = first["id"]
                else:
                    first["binding"] = {"pipeline_name": "one", "dataset_name": "one"}
                    second["binding"] = {"pipeline_name": "two", "dataset_name": "two"}
                    second["binding"][field] = "one"
                with self.assertRaises(ConfigurationError):
                    self.parse(config)

    def test_rejects_unknown_fields_bad_versions_and_naive_dates(self):
        for mutate in (
            lambda c: c.update(schema_version=True),
            lambda c: c.update(schema_version=2),
            lambda c: c.update(unrecognized=True),
            lambda c: c["connections"][0]["settings"].update(start_date="2020-01-01"),
            lambda c: c["connections"][0]["settings"].update(username=123),
            lambda c: c["connections"][0]["settings"].update(access_token="secret"),
            lambda c: c["connections"][0].update(integration="unknown"),
            lambda c: c["connections"][0].update(id="../escape"),
            lambda c: c["connections"][1]["settings"].update(cutoff="2026-02-30"),
            lambda c: c["connections"][0].update(credentials={}),
            lambda c: c["connections"][1].update(
                credentials={"unexpected": {"provider": "env", "key": "TOKEN"}}
            ),
            lambda c: c["connections"][0]["credentials"].update(
                access_token="inline-secret"
            ),
        ):
            config = example()
            mutate(config)
            with self.assertRaises(ConfigurationError):
                self.parse(config)

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaises(ConfigurationError):
            parse_miroir('{"id":"one","id":"two"}')

    def test_cli_error_does_not_echo_secret_values_or_unknown_keys(self):
        config = example()
        marker = "SENSITIVE_VALUE_MUST_NOT_APPEAR"
        config["connections"][0]["settings"]["username"] = {marker: marker}
        config["connections"][0]["settings"][marker] = marker
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(config))
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["--config", str(path), "validate"])
        self.assertEqual(code, 2)
        self.assertNotIn(marker, out.getvalue() + err.getvalue())
        self.assertIn("settings", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_cli_missing_file_is_a_safe_error(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            redirect_stderr(io.StringIO()) as err,
        ):
            code = main(["--config", str(Path(tmp) / "missing"), "validate"])
        self.assertEqual(code, 2)
        self.assertIn("Cannot read", err.getvalue())

    def test_cli_does_not_load_runtimes_or_fetch_secrets(self):
        script = """
import os, socket, subprocess, sys
from src.cli.main import main

def forbidden(*args, **kwargs):
    raise AssertionError('read-only CLI attempted external I/O')
socket.socket = forbidden
subprocess.Popen = forbidden
os.environ['LISTENBRAINZ__ACCESS_TOKEN'] = 'SECRET_SENTINEL'
assert main(['--config', 'examples/miroir.json', 'connections']) == 0
assert 'dlt' not in sys.modules
assert 'prefect' not in sys.modules
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertNotIn("SECRET_SENTINEL", result.stdout + result.stderr)
        self.assertEqual(len(json.loads(result.stdout)), 2)


if __name__ == "__main__":
    unittest.main()
