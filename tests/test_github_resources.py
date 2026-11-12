"""Modular GitHub resources, with real dlt loads and intercepted HTTP requests."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import dlt
import requests
from dlt.common.pendulum import pendulum
from dlt.extract.exceptions import ResourceExtractionError
from dlt.pipeline.exceptions import PipelineStepFailed

from src.core.runner import failure_kind
from src.integrations.errors import IntegrationError
from src.integrations.github import DEFINITION
from src.integrations.github.source import github_source


class GitHubResourceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.calls = []
        self.login = "octo-cat"
        self.fail_path = None
        self.date = "2026-11-01T12:00:00Z"
        self.account_body = None
        self.response_headers = {}
        self.repositories = ["octo-cat/project"]
        self.rows_by_path = {}
        self.queued_statuses = []
        self.links_by_path = {}
        self.graphql_requests = []
        self.calendar_count = 2
        self.pipeline = dlt.pipeline(
            pipeline_name="github",
            pipelines_dir=str(self.root / "state"),
            destination=dlt.destinations.duckdb(str(self.root / "github.duckdb")),
            dataset_name="github_data",
        )
        http = patch.object(requests.Session, "send", self.send)
        http.start()
        self.addCleanup(http.stop)
        pause = patch("src.integrations.github.client.time.sleep")
        pause.start()
        self.addCleanup(pause.stop)

    def send(self, request, **kwargs):
        url = urlparse(request.url)
        if url.hostname != "api.github.com":
            response = requests.Response()
            response.status_code = 200
            response._content = b"{}"
            return response
        query = parse_qs(url.query)
        self.calls.append((url.path, query))
        body = []
        if url.path == "/user":
            body = (
                self.account_body
                if self.account_body is not None
                else {"login": self.login, "created_at": "2026-10-01T00:00:00Z"}
            )
        elif url.path == "/user/repos":
            body = [
                {"id": n, "full_name": name}
                for n, name in enumerate(self.repositories, 1)
            ]
        elif url.path.endswith("/branches"):
            body = [{"name": "main"}, {"name": "other"}]
        elif url.path.endswith("/commits") and not url.path.startswith("/search"):
            body = [{"sha": "abc"}]
        elif "/commits/" in url.path:
            body = {
                "sha": "abc",
                "html_url": f"https://github.com{url.path}",
                "stats": {"additions": 2, "deletions": 1},
                "files": [{"patch": "not retained"}],
            }
        elif url.path == "/user/starred":
            body = [{"starred_at": self.date, "repo": {"id": 2}}]
        elif url.path.endswith("/events"):
            body = [{"id": "event1", "payload": {"action": "opened"}}]
        elif url.path == "/graphql":
            self.graphql_requests.append(json.loads(request.body)["variables"])
            body = {
                "data": {
                    "user": {
                        "contributionsCollection": {
                            "contributionCalendar": {
                                "weeks": [
                                    {
                                        "contributionDays": [
                                            {
                                                "date": "2026-11-01",
                                                "contributionCount": self.calendar_count,
                                            }
                                        ]
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        elif url.path.startswith("/search"):
            body = {
                "total_count": 1,
                "items": [
                    {"sha": "abc", "repository": {"full_name": "other/project"}}
                    if url.path == "/search/commits"
                    else {
                        "html_url": "https://github.com/other/project/issues/1",
                        "id": 3,
                    }
                ],
            }
        elif url.path.endswith(("/pulls", "/issues", "/issues/comments", "/releases")):
            body = [
                {"html_url": f"https://github.com{url.path}/1", "updated_at": self.date}
            ]
        else:
            self.fail(f"Unexpected request: {request.url}")
        body = self.rows_by_path.get(url.path, body)
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.status_code = 403 if url.path == self.fail_path else 200
        if self.queued_statuses:
            response.status_code = self.queued_statuses.pop(0)
        response._content = json.dumps(body).encode()
        response.headers["Content-Type"] = "application/json"
        response.headers.update(self.response_headers)
        if url.path in self.links_by_path and "page" not in query:
            response.headers["Link"] = self.links_by_path[url.path]
        return requests.hooks.dispatch_hook(
            "response", request.hooks, response, **kwargs
        )

    def load(self, *resources):
        with requests.Session() as session:
            source = github_source("octo-cat", "fake", session)
            if resources:
                source = source.with_resources(*resources)
            return self.pipeline.run(source)

    def states(self):
        return self.pipeline.state["sources"]["github"]["resources"]

    def test_assembly_is_lazy_and_matches_catalog_and_existing_hints(self):
        with requests.Session() as session:
            source = github_source("octo-cat", "fake", session)
        self.assertEqual(self.calls, [])
        self.assertEqual(source.name, "github")
        self.assertEqual(
            set(source.selected_resources), {r.name for r in DEFINITION.resources}
        )
        self.assertFalse(source.resources["owned_repositories"].selected)
        for name, resource in source.selected_resources.items():
            schema = resource.compute_table_schema()
            self.assertEqual(schema["name"], name)
            self.assertEqual(schema["write_disposition"], "merge")
            keys = [
                c["name"] for c in schema["columns"].values() if c.get("primary_key")
            ]
            expected = {
                "repos": "id",
                "events": "id",
                "stars": "repo__id",
                "contributions_daily": "date",
            }.get(name, "html_url")
            self.assertEqual(keys, [expected])

    def test_all_resources_load_through_the_same_source(self):
        self.load()
        tables = self.pipeline.default_schema.tables
        for resource in DEFINITION.resources:
            self.assertIn(resource.name, tables)
        self.assertIn("last_run", self.states()["commits"])
        self.assertIn("last_run", self.states()["external_commits"])
        self.assertEqual(
            self.states()["contributions_daily"]["incremental"]["date"]["last_value"],
            "2026-11-01",
        )
        self.assertNotIn("commits__files", tables)
        self.assertEqual(sum(p == "/user" for p, q in self.calls), 1)

    def test_selection_fetches_dependencies_without_loading_them_and_resumes(self):
        self.load("commits", "issues")
        states = self.states()
        committed = states["commits"]["last_run"]
        self.assertEqual(sum(p == "/user/repos" for p, q in self.calls), 1)
        self.assertNotIn("repos", self.pipeline.default_schema.tables)
        self.assertEqual(sum(p.endswith("/commits/abc") for p, q in self.calls), 1)
        with self.pipeline.sql_client() as sql:
            self.assertEqual(sql.execute_sql("select count(*) from commits")[0][0], 1)
        self.calls.clear()
        self.load("stars")
        self.assertEqual(self.states()["commits"]["last_run"], committed)
        self.assertEqual([p for p, q in self.calls], ["/user", "/user/starred"])
        self.calls.clear()
        # A fresh worker restores the same source/resource state from dlt.
        self.pipeline = dlt.pipeline(
            pipeline_name="github",
            pipelines_dir=str(self.root / "fresh_state"),
            destination=dlt.destinations.duckdb(str(self.root / "github.duckdb")),
            dataset_name="github_data",
        )
        self.load("commits")
        since = next(q["since"][0] for p, q in self.calls if p.endswith("/commits"))
        self.assertEqual(
            pendulum.parse(since),
            pendulum.parse(committed).subtract(days=3).replace(microsecond=0),
        )
        with self.pipeline.sql_client() as sql:
            self.assertEqual(sql.execute_sql("select count(*) from commits")[0][0], 1)

    def test_account_mismatch_fails_before_any_resource_requests(self):
        self.login = "someone-else"
        with self.assertRaises(PipelineStepFailed) as caught:
            self.load("stars")
        self.assertIn("different account", str(caught.exception))
        self.assertEqual(failure_kind(caught.exception), "credentials_rejected")
        self.assertEqual([p for p, q in self.calls], ["/user"])

    def test_failed_extraction_does_not_advance_committed_cursor(self):
        self.load("issues")
        committed = self.states()["issues"]["incremental"]["updated_at"].copy()
        self.fail_path = "/repos/octo-cat/project/issues"
        with (
            patch.dict("os.environ", {"RUNTIME__REQUEST_MAX_ATTEMPTS": "1"}),
            self.assertRaises(PipelineStepFailed),
        ):
            self.load("issues")
        self.assertEqual(
            self.states()["issues"]["incremental"]["updated_at"], committed
        )

    def test_account_responses_have_safe_classified_errors(self):
        from src.integrations.github.client import GitHubClient

        for body in (
            [],
            {},
            {"login": 1},
            {"login": "octo-cat"},
            {"login": "octo-cat", "created_at": "invalid"},
            {"login": "octo-cat", "created_at": None},
        ):
            self.account_body = body
            with self.subTest(body=body), requests.Session() as session:
                api = GitHubClient("octo-cat", "fake", session)
                with self.assertRaises(IntegrationError) as caught:
                    _ = api.authenticated_client
                self.assertEqual(caught.exception.kind, "unexpected_response")
                self.assertIn("unexpected", str(caught.exception))
        self.account_body = None
        self.fail_path = "/user"
        self.response_headers = {"x-ratelimit-remaining": "0"}
        with self.assertRaises(PipelineStepFailed) as caught:
            self.load("stars")
        self.assertEqual(failure_kind(caught.exception), "rate_limited")

    def seed_legacy(self, *names, stamp="2026-11-03T12:00:00Z"):
        @dlt.source(name="github")
        def legacy_source():
            def seed(name):
                @dlt.resource(
                    name=name,
                    write_disposition="merge",
                    primary_key="html_url" if name != "contributions_daily" else "date",
                )
                def resource():
                    key = "last_date" if name == "contributions_daily" else "last_run"
                    dlt.current.resource_state()[key] = (
                        stamp[:10] if key == "last_date" else stamp
                    )
                    yield []

                return resource

            return [seed(name) for name in names]

        self.pipeline.run(legacy_source())

    def test_legacy_watermarks_migrate_once_and_resume_from_native_state(self):
        names = ("issues", "issue_comments", "pull_requests", "contributions_daily")
        self.seed_legacy(*names)
        self.date = "2026-11-04T12:00:00Z"
        self.load(*names)
        for name in names[:-1]:
            state = self.states()[name]
            self.assertEqual(state["last_run"], "2026-11-03T12:00:00Z")
            self.assertEqual(
                state["incremental"]["updated_at"]["last_value"], self.date
            )
        for path, query in self.calls:
            if path.endswith(("/issues", "/issues/comments")):
                self.assertEqual(query["since"], ["2026-10-31T12:00:00Z"])
        self.calls.clear()
        self.pipeline = dlt.pipeline(
            pipeline_name="github",
            pipelines_dir=str(self.root / "fresh_native"),
            destination=dlt.destinations.duckdb(str(self.root / "github.duckdb")),
            dataset_name="github_data",
        )
        self.load("issues")
        query = next(q for p, q in self.calls if p.endswith("/issues"))
        self.assertEqual(query["since"], ["2026-11-01T12:00:00Z"])

    def test_failed_migration_is_not_committed(self):
        self.seed_legacy("issues")
        before = json.loads(json.dumps(self.states()))
        self.fail_path = "/repos/octo-cat/project/issues"
        with self.assertRaises(PipelineStepFailed):
            self.load("issues")
        self.assertEqual(self.states(), before)
        self.fail_path = None
        self.date = "2026-11-04T12:00:00Z"
        self.load("issues")
        query = [q for p, q in self.calls if p.endswith("/issues")][-1]
        self.assertEqual(query["since"], ["2026-10-31T12:00:00Z"])

    def test_transformers_keep_cross_repository_order_and_refresh_lagged_rows(self):
        self.repositories = ["octo-cat/newer", "octo-cat/older"]
        for repo, date in zip(
            self.repositories, ("2026-11-10", "2026-11-09"), strict=True
        ):
            for suffix in ("issues", "issues/comments", "pulls"):
                path = f"/repos/{repo}/{suffix}"
                self.rows_by_path[path] = [
                    {
                        "html_url": f"https://github.com{path}/1",
                        "updated_at": f"{date}T12:00:00Z",
                        "title": "original",
                    }
                ]
        self.load("issues", "issue_comments", "pull_requests", "releases")
        for table in ("issues", "issue_comments", "pull_requests"):
            with self.pipeline.sql_client() as sql:
                self.assertEqual(
                    sql.execute_sql(f"select count(*) from {table}")[0][0], 2
                )
        self.assertNotIn("owned_repositories", self.pipeline.default_schema.tables)
        self.assertNotIn("repos", self.pipeline.default_schema.tables)
        self.calls.clear()
        for path, rows in self.rows_by_path.items():
            rows[0]["title"] = "refreshed"
        self.load("issues", "issue_comments", "pull_requests")
        for table in ("issues", "issue_comments", "pull_requests"):
            with self.pipeline.sql_client() as sql:
                self.assertEqual(
                    sql.execute_sql(
                        f"select count(*) from {table} where title = 'refreshed'"
                    )[0][0],
                    2,
                )
        since = [
            q["since"]
            for p, q in self.calls
            if p.endswith(("/issues", "/issues/comments"))
        ]
        self.assertEqual(since, [["2026-11-07T12:00:00Z"]] * 4)

    def test_dlt_client_retries_only_rate_limited_forbidden_responses(self):
        from src.integrations.github import GitHubSettings, open_fetch

        for status, headers, expected_requests in (
            (403, {"x-ratelimit-remaining": "0"}, 2),
            (403, {"Retry-After": "0"}, 2),
            (429, {}, 2),
            (503, {}, 2),
            (403, {}, 1),
            (401, {}, 1),
        ):
            self.calls.clear()
            self.queued_statuses = [status, 200]
            self.response_headers = headers
            with (
                self.subTest(status=status, headers=headers),
                patch.dict("os.environ", {"RUNTIME__REQUEST_MAX_ATTEMPTS": "2"}),
                open_fetch(
                    GitHubSettings(username="octo-cat"), {"access_token": "fake"}
                ) as source,
            ):
                if expected_requests == 1:
                    with self.assertRaises(ResourceExtractionError):
                        list(source.with_resources("stars"))
                else:
                    self.assertEqual(len(list(source.with_resources("stars"))), 1)
            self.assertEqual(
                sum(p == "/user" for p, q in self.calls), expected_requests
            )

    def test_declarative_resources_keep_pagination_headers_and_scope(self):
        self.links_by_path = {
            "/user/starred": '<https://api.github.com/user/starred?page=2>; rel="next"'
        }
        self.load("repos", "stars", "events")
        queries = [q for p, q in self.calls if p == "/user/starred"]
        self.assertEqual(len(queries), 2)
        self.assertEqual(queries[0]["per_page"], ["100"])
        self.assertEqual(queries[1]["page"], ["2"])
        repo_query = next(q for p, q in self.calls if p == "/user/repos")
        self.assertEqual(
            repo_query["affiliation"], ["owner,collaborator,organization_member"]
        )
        self.assertEqual(
            self.pipeline.default_schema.tables["events"]["columns"]["payload"][
                "data_type"
            ],
            "json",
        )
        self.calls.clear()
        self.load("releases")
        repo_query = next(q for p, q in self.calls if p == "/user/repos")
        self.assertEqual(repo_query["affiliation"], ["owner"])

    def test_empty_migrated_resource_retains_watermark_and_lag(self):
        self.seed_legacy("issues")
        path = "/repos/octo-cat/project/issues"
        self.rows_by_path[path] = []
        self.load("issues")
        self.assertEqual(
            self.states()["issues"]["incremental"]["updated_at"]["last_value"],
            "2026-11-03T12:00:00Z",
        )
        self.calls.clear()
        self.rows_by_path[path] = [
            {
                "html_url": "https://github.com/issue/1",
                "updated_at": "2026-11-02T12:00:00Z",
            }
        ]
        self.load("issues")
        query = next(q for p, q in self.calls if p == path)
        self.assertEqual(query["since"], ["2026-10-31T12:00:00Z"])
        with self.pipeline.sql_client() as sql:
            self.assertEqual(sql.execute_sql("select count(*) from issues")[0][0], 1)

    def test_calendar_migration_and_lag_refresh_existing_days(self):
        self.seed_legacy("contributions_daily")
        self.load("contributions_daily")
        self.assertEqual(self.graphql_requests[0]["from"], "2026-10-31T00:00:00+00:00")
        self.graphql_requests.clear()
        self.calendar_count = 7
        self.load("contributions_daily")
        self.assertEqual(self.graphql_requests[0]["from"], "2026-10-31T00:00:00+00:00")
        with self.pipeline.sql_client() as sql:
            self.assertEqual(
                sql.execute_sql("select contribution_count from contributions_daily"),
                [(7,)],
            )

    def test_pull_request_early_stop_is_local_to_each_repository(self):
        self.seed_legacy("pull_requests", stamp="2026-11-10T12:00:00Z")
        self.repositories = ["octo-cat/newer", "octo-cat/older"]
        for name, date in zip(
            self.repositories, ("2026-11-10", "2026-11-09"), strict=True
        ):
            path = f"/repos/{name}/pulls"
            self.rows_by_path[path] = [
                {
                    "html_url": f"https://github.com/{name}/pull/1",
                    "updated_at": f"{date}T12:00:00Z",
                },
                {
                    "html_url": f"https://github.com/{name}/pull/2",
                    "updated_at": "2026-11-01T12:00:00Z",
                },
            ]
            self.links_by_path[path] = (
                f'<https://api.github.com{path}?page=2>; rel="next"'
            )
        self.load("pull_requests")
        paths = [(p, q) for p, q in self.calls if p.endswith("/pulls")]
        self.assertEqual(len(paths), 2)
        self.assertTrue(all("page" not in q for p, q in paths))
        with self.pipeline.sql_client() as sql:
            self.assertEqual(
                sql.execute_sql("select count(*) from pull_requests"), [(2,)]
            )
