"""Exercise real dlt sources against synthetic inputs and a local warehouse.

HTTP is intercepted at the requests transport boundary. No live service credentials
or production destination are used. These tests validate adapter compatibility, not
an implemented miroir execution service or artifact inbox.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import dlt
import duckdb
import requests

from src.core.config import parse_miroir
from src.integrations.sources import open_fetch, open_import
from tests.test_miroir import example


class ReferenceSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / "warehouse.duckdb"

    def pipeline(self, connection, state_dir="state"):
        return dlt.pipeline(
            pipeline_name=connection.binding.pipeline_name,
            pipelines_dir=str(self.root / state_dir),
            destination=dlt.destinations.duckdb(credentials=str(self.database)),
            dataset_name=connection.binding.dataset_name,
        )

    def count(self, connection, table):
        with duckdb.connect(str(self.database)) as db:
            return db.execute(
                f'SELECT count(*) FROM "{connection.binding.dataset_name}"."{table}"'
            ).fetchone()[0]

    def test_api_accounts_merge_independently_and_restore_committed_state(self):
        config = example()
        config["connections"] = config["connections"][:1]
        second = copy.deepcopy(config["connections"][0])
        second["id"] = "other_music"
        second["settings"]["username"] = "other_user"
        config["connections"].append(second)
        connections = parse_miroir(json.dumps(config)).connections
        requests_seen = []

        def respond(session, request, **kwargs):
            parsed = urlsplit(request.url)
            self.assertEqual(parsed.hostname, "api.listenbrainz.org")
            username = parsed.path.split("/")[3]
            self.assertIn(username, ("example_user", "other_user"))
            self.assertEqual(
                request.headers["Authorization"], f"Bearer token-{username}"
            )
            requests_seen.append((username, parse_qs(parsed.query)))
            response = requests.Response()
            response.status_code = 200
            response.request = request
            response.url = request.url
            response.headers["Content-Type"] = "application/json"
            response._content = json.dumps(
                {
                    "payload": {
                        "listens": [
                            {
                                "listened_at": 1710000000,
                                "recording_msid": "same-source-id",
                                "track_metadata": {"track_name": username},
                            }
                        ]
                    }
                }
            ).encode()
            return response

        with patch.object(requests.Session, "send", respond):
            for connection in connections:
                credentials = {"access_token": f"token-{connection.settings.username}"}
                with open_fetch(connection, credentials) as source:
                    self.pipeline(connection).run(source)
                self.assertEqual(self.count(connection, "listens"), 1)
            # A fresh local working directory must restore the committed cursor from
            # the destination, then replay the boundary record without duplicating it.
            connection = connections[0]
            with open_fetch(
                connection, {"access_token": "token-example_user"}
            ) as source:
                self.pipeline(connection, "restarted-state").run(source)
        self.assertEqual(requests_seen[-1][1]["min_ts"], ["1709999999"])
        for connection in connections:
            self.assertEqual(self.count(connection, "listens"), 1)
            with duckdb.connect(str(self.database)) as db:
                name = db.execute(
                    f'SELECT track_metadata__track_name FROM "{connection.binding.dataset_name}".listens'
                ).fetchone()[0]
            self.assertEqual(name, connection.settings.username)

    def test_health_missing_source_is_unknown_and_reimport_merges(self):
        payload = {
            "data": {
                "metrics": [
                    {
                        "name": "steps",
                        "units": "count",
                        "data": [
                            {"date": "2026-06-03 12:00:00 -0400", "qty": 10},
                            {"date": "2026-06-04 12:00:00 -0400", "qty": 20},
                        ],
                    }
                ]
            }
        }
        path = self.root / "export.json"
        path.write_text(json.dumps(payload))
        config = example()
        config["connections"] = config["connections"][1:]
        connection = parse_miroir(json.dumps(config)).connections[0]
        cutoff = connection.settings.cutoff
        for role in ("historical", "ongoing", "ongoing"):
            options = {"role": role, "cutoff": cutoff}
            with open_import(
                connection, "health_auto_export_json", str(path), options
            ) as source:
                self.pipeline(connection).run(source)
        # One row each side of the cutoff; the repeated import merged.
        self.assertEqual(self.count(connection, "metrics"), 2)
        with duckdb.connect(str(self.database)) as db:
            sources = db.execute(
                f'SELECT DISTINCT source FROM "{connection.binding.dataset_name}".metrics'
            ).fetchall()
        self.assertEqual(sources, [("unknown",)])

    def test_source_requires_explicit_credentials(self):
        connection = parse_miroir(json.dumps(example())).connections[0]
        with self.assertRaises(ValueError), open_fetch(connection, {}):
            pass
        with (
            self.assertRaises(ValueError),
            open_fetch(connection, {"access_token": ""}),
        ):
            pass


if __name__ == "__main__":
    unittest.main()
