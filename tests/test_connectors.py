"""Connector contracts: settings, resource selection, checks, file formats. No network."""

import gzip
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import requests

from src.analyses.catalog import PACKAGES
from src.core.analysis import analysis_plan
from src.core.artifacts import submit_artifact
from src.core.config import ConfigurationError, parse_miroir
from src.core.operations import MetadataStore, OperationError
from src.integrations.checks import CheckFailed
from src.integrations.registry import INTEGRATIONS
from src.integrations.sources import open_fetch
from src.integrations.validation import InvalidArtifact

SECRET = {"provider": "env", "key": "UNUSED"}


def github(**overrides):
    return {
        "id": "code",
        "integration": "github",
        "settings": {"username": "octo-cat"},
        "credentials": {"access_token": SECRET},
        **overrides,
    }


def parse(*connections):
    return parse_miroir(
        json.dumps({"schema_version": 1, "id": "tests", "connections": connections})
    )


def places(path: Path) -> Path:
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(
            "create table moz_places (id integer primary key, url text);"
            "create table moz_historyvisits (id integer primary key, "
            "place_id integer, visit_date integer);"
        )
    return path


class ConnectorContractTests(unittest.TestCase):
    def test_every_integration_recommends_a_cataloged_analysis(self):
        for definition in INTEGRATIONS.values():
            self.assertIn(definition.analysis, PACKAGES, definition.name)
            self.assertTrue(definition.fetch or definition.formats)

    def test_resource_selection_is_validated_and_applied(self):
        for bad in ([], ["repos", "repos"], ["not_a_resource"]):
            with self.assertRaises(ConfigurationError):
                parse(github(resources=bad))
        connection = parse(github(resources=["repos", "stars"])).connections[0]
        self.assertEqual(connection.summary()["resources"], ["repos", "stars"])
        with (
            patch.object(
                requests.Session,
                "send",
                side_effect=AssertionError("Source assembly made a request"),
            ),
            open_fetch(connection, {"access_token": "t"}) as source,
        ):
            self.assertEqual(sorted(source.selected_resources), ["repos", "stars"])
        # Without a selection, every declared resource stays selected.
        connection = parse(github()).connections[0]
        with (
            patch.object(
                requests.Session,
                "send",
                side_effect=AssertionError("Source assembly made a request"),
            ),
            open_fetch(connection, {"access_token": "t"}) as source,
        ):
            self.assertEqual(
                sorted(source.selected_resources),
                sorted(r.name for r in connection.integration.resources),
            )

    def test_analysis_parameters_belong_to_the_analysis_not_credentials(self):
        def configure(parameters):
            return parse_miroir(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "tests",
                        "connections": [github()],
                        "analyses": [
                            {
                                "id": "code",
                                "package": "github",
                                "inputs": {"github": "code"},
                                "parameters": parameters,
                            }
                        ],
                    }
                )
            )

        miroir = configure({"author_emails": [" Me@X.org", "me@x.org"]})
        variables = analysis_plan(miroir.analyses[0]).variables
        # Identity comes from the linked connection, emails from the analysis.
        self.assertEqual(variables["github_login"], "octo-cat")
        self.assertEqual(variables["github_author_emails"], ["me@x.org"])
        for bad in ({"author_emails": ["nope"]}, {"unknown": 1}):
            with self.assertRaises(ConfigurationError):
                configure(bad)
        # Connections no longer carry analysis settings once analyses exist.
        with self.assertRaises(ConfigurationError):
            parse_miroir(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "tests",
                        "connections": [github(analysis={"timezone": "UTC"})],
                        "analyses": [],
                    }
                )
            )

    def test_github_check_rejects_a_token_for_another_account(self):
        settings = parse(github()).connections[0].settings
        check = INTEGRATIONS["github"].check

        class Response:
            status_code = 200

            def __init__(self, login):
                self.login = login

            def json(self):
                return {"login": self.login}

        with patch("requests.get", return_value=Response("someone-else")):
            with self.assertRaises(CheckFailed) as caught:
                check(settings, {"access_token": "t"})
            self.assertTrue(caught.exception.rejected)
        with patch("requests.get", return_value=Response("Octo-Cat")):
            self.assertEqual(
                check(settings, {"access_token": "t"}), "GitHub account access verified"
            )

    def test_github_checks_classify_limits_and_malformed_accounts_safely(self):
        settings = parse(github()).connections[0].settings
        check = INTEGRATIONS["github"].check
        for status, headers, body, rejected, message in (
            (403, {"x-ratelimit-remaining": "0"}, b"{}", False, "rate limit"),
            (403, {"Retry-After": "10"}, b"{}", False, "rate limit"),
            (429, {}, b"{}", False, "rate limit"),
            (401, {}, b"{}", True, "rejected access"),
            (403, {}, b"{}", True, "rejected access"),
            (200, {}, b'{"login": 123}', False, "unexpected"),
            (200, {}, b"{}", False, "unexpected"),
            (200, {}, b"<html>token-value</html>", False, "unexpected"),
        ):
            response = requests.Response()
            response.status_code = status
            response.headers.update(headers)
            response._content = body
            with (
                self.subTest(status=status, headers=headers, body=body),
                patch("requests.get", return_value=response),
            ):
                with self.assertRaises(CheckFailed) as caught:
                    check(settings, {"access_token": "t"})
                self.assertEqual(caught.exception.rejected, rejected)
                self.assertIn(message, str(caught.exception))
                self.assertNotIn("token-value", str(caught.exception))
        response = requests.Response()
        response.status_code = 200
        response.headers["x-ratelimit-remaining"] = "0"
        response._content = b'{"login": "octo-cat"}'
        with patch("requests.get", return_value=response):
            self.assertEqual(
                check(settings, {"access_token": "t"}), "GitHub account access verified"
            )

    def test_raw_http_errors_use_the_same_failure_classification(self):
        from src.core.runner import failure_kind

        for status, headers, expected in (
            (403, {"Retry-After": "10"}, "rate_limited"),
            (429, {}, "rate_limited"),
            (403, {}, "credentials_rejected"),
            (500, {}, None),
        ):
            response = requests.Response()
            response.status_code = status
            response.headers.update(headers)
            self.assertEqual(
                failure_kind(requests.HTTPError(response=response)), expected
            )

    def test_checks_fail_readably_on_unexpected_bodies(self):
        music = {
            "id": "music",
            "integration": "listenbrainz",
            "settings": {"username": "me", "start_date": "2020-01-01T00:00:00Z"},
            "credentials": {"access_token": SECRET},
        }
        rides = {
            "id": "rides",
            "integration": "bikeshare",
            "settings": {},
            "credentials": {"member_id": SECRET, "authorization_token": SECRET},
        }
        credentials = {
            "access_token": "t",
            "member_id": "m",
            "authorization_token": "t",
        }
        for connection in parse(music, github(), rides).connections:
            definition = connection.integration
            assert definition.check is not None
            # ListenBrainz answers clients it suspects with an HTML page and 200.
            for body in (b"<html>Are you a bot?</html>", b"[]"):
                response = requests.Response()
                response.status_code = 200
                response._content = body
                with (
                    self.subTest(definition.name, body=body),
                    patch("requests.get", return_value=response),
                ):
                    with self.assertRaisesRegex(CheckFailed, "unexpected") as caught:
                        definition.check(connection.settings, credentials)
                    self.assertFalse(caught.exception.rejected)


class BrowserSnapshotTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.validate = INTEGRATIONS["browser_history"].format().validate

    def test_plain_and_gzip_places_snapshots_are_one_format(self):
        plain = places(self.root / "places.sqlite")
        self.validate(plain)
        compressed = self.root / "places.sqlite.gz"
        compressed.write_bytes(gzip.compress(plain.read_bytes()))
        self.validate(compressed)

    def test_other_files_and_databases_are_rejected(self):
        text = self.root / "notes.txt"
        text.write_text("not sqlite")
        other = self.root / "other.sqlite"
        with closing(sqlite3.connect(other)) as db, db:
            db.execute("create table moz_places (id integer primary key)")
        for path in (text, other):
            with self.assertRaises(InvalidArtifact):
                self.validate(path)

    def test_truncated_or_corrupt_gzip_snapshots_are_rejected(self):
        compressed = gzip.compress(places(self.root / "places.sqlite").read_bytes())
        for name, data in (
            ("half-written", compressed[: len(compressed) // 2]),
            (
                "corrupt",
                compressed[:10] + bytes(len(compressed) - 18) + compressed[-8:],
            ),
            ("bad-checksum", compressed[:-8] + bytes(8)),
        ):
            path = self.root / f"{name}.sqlite.gz"
            path.write_bytes(data)
            with self.subTest(name), self.assertRaises(InvalidArtifact):
                self.validate(path)

    def test_snapshots_take_no_import_options(self):
        miroir = parse(
            {"id": "browsing", "integration": "browser_history", "settings": {}}
        )
        store = MetadataStore(f"sqlite:///{self.root / 'meta.db'}")
        store.migrate()
        self.addCleanup(store.engine.dispose)
        with self.assertRaisesRegex(OperationError, "Invalid import options: role"):
            submit_artifact(
                miroir,
                miroir.connections[0],
                places(self.root / "places.sqlite"),
                {"role": "historical"},
                store=store,
                root=self.root / "artifacts",
            )


if __name__ == "__main__":
    unittest.main()
