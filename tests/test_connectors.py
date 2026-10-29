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

from src.analyses.catalog import ANALYSES
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


def github_user(session, request, **kwargs):
    """The only request made while opening a GitHub source: the account check."""
    assert request.url.endswith("/user"), request.url
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"login": "octo-cat", "created_at": "2020-01-01T00:00:00Z"}'
    response.request = request
    return response


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
            self.assertIn(definition.analysis, ANALYSES, definition.name)
            self.assertTrue(definition.fetch or definition.formats)

    def test_resource_selection_is_validated_and_applied(self):
        for bad in ([], ["repos", "repos"], ["not_a_resource"]):
            with self.assertRaises(ConfigurationError):
                parse(github(resources=bad))
        connection = parse(github(resources=["repos", "stars"])).connections[0]
        self.assertEqual(connection.summary()["resources"], ["repos", "stars"])
        with (
            patch.object(requests.Session, "send", github_user),
            open_fetch(connection, {"access_token": "t"}) as source,
        ):
            self.assertEqual(sorted(source.selected_resources), ["repos", "stars"])
        # Without a selection, every declared resource stays selected.
        connection = parse(github()).connections[0]
        with (
            patch.object(requests.Session, "send", github_user),
            open_fetch(connection, {"access_token": "t"}) as source,
        ):
            self.assertEqual(
                sorted(source.selected_resources),
                sorted(r.name for r in connection.integration.resources),
            )

    def test_analysis_parameters_belong_to_the_analysis_not_credentials(self):
        connection = parse(
            github(
                analysis={"parameters": {"author_emails": [" Me@X.org", "me@x.org"]}}
            )
        ).connections[0]
        variables = analysis_plan(connection).variables
        self.assertEqual(variables["github_login"], "octo-cat")
        self.assertEqual(variables["github_author_emails"], ["me@x.org"])
        with self.assertRaises(ConfigurationError):
            parse(github(analysis={"parameters": {"author_emails": ["nope"]}}))
        music = {
            "id": "music",
            "integration": "listenbrainz",
            "settings": {"username": "u", "start_date": "2020-01-01T00:00:00Z"},
            "credentials": {"access_token": SECRET},
            "analysis": {"parameters": {"author_emails": []}},
        }
        with self.assertRaises(ConfigurationError):
            parse(music)

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
