"""Explicit analysis linking, independent builds, and schema-6 continuity."""

import copy
import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from src.core.analysis import analysis_plan
from src.core.config import ConfigurationError, parse_miroir, upgrade_raw
from src.core.operations import attempts, schema_info
from src.core.runner import execute_operation, run_connection
from src.core.service import InlineExecutor
from tests.test_miroir import example
from tests.test_web import BEARER, WebFixture


class AnalysisConfigTests(unittest.TestCase):
    def test_upgrade_preserves_namespaces_parameters_dashboards_and_source_revision(
        self,
    ):
        raw = example()
        raw["connections"][0]["analysis"] = {"timezone": "America/Toronto"}
        raw["connections"][0]["dashboards"] = {"Music": 32}
        before = parse_miroir(json.dumps(raw))
        upgrade_raw(raw)
        after = parse_miroir(json.dumps(raw))
        self.assertNotIn("analysis", raw["connections"][0])
        self.assertEqual(raw["analyses"][0]["dashboards"], {"Music": 32})
        self.assertEqual(before.connections, after.connections)
        plan = analysis_plan(after.analyses[0])
        identity = hashlib.sha256(
            json.dumps(["personal", "music"]).encode()
        ).hexdigest()[:16]
        self.assertEqual(plan.relations[0], f"miroir_music_{identity}_music.daily")
        self.assertEqual(plan.variables["timezone"], "America/Toronto")
        self.assertEqual(plan, analysis_plan(before.analyses[0]))

    def test_analyses_list_is_authoritative_and_links_are_validated(self):
        raw = example()
        raw["analyses"] = []
        self.assertEqual(parse_miroir(json.dumps(raw)).analyses, ())
        valid = {
            "id": "combined",
            "package": "crossovers",
            "inputs": {"health": "health"},
        }
        for invalid in (
            {**valid, "package": "missing"},
            {**valid, "inputs": {}},
            {**valid, "inputs": {"health": "music"}},
            {**valid, "inputs": {"health": "missing"}},
            {**valid, "inputs": {"health": "health", "extra": "music"}},
            {**valid, "parameters": {"unexpected": True}},
            {**valid, "timezone": "bad-zone"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ConfigurationError):
                parse_miroir(json.dumps({**raw, "analyses": [invalid]}))
        with self.assertRaises(ConfigurationError):
            parse_miroir(json.dumps({**raw, "analyses": [valid, valid]}))
        raw["analyses"] = [valid]
        plan = analysis_plan(parse_miroir(json.dumps(raw)).analyses[0])
        self.assertEqual(plan.variables["miroir_inputs"], ["health"])
        self.assertEqual(
            plan.variables["miroir_unlinked_sources"], ["listenbrainz", "bikeshare"]
        )

    def test_invalid_legacy_shapes_are_rejected_without_leaking_values(self):
        for invalid in ("SECRET", {"unexpected_SECRET": True}, {"parameters": []}):
            raw = example()
            raw["connections"][0]["analysis"] = invalid
            with self.assertRaises(ConfigurationError) as caught:
                parse_miroir(json.dumps(raw))
            self.assertNotIn("SECRET", str(caught.exception))
        for invalid in (5, "SECRET"):
            with self.assertRaises(ConfigurationError):
                parse_miroir(
                    json.dumps(
                        {"schema_version": 1, "id": "test", "connections": invalid}
                    )
                )


class AnalysisServiceTests(WebFixture):
    def revision(self):
        return self.service._raw_config()[1]

    def add_crossovers(self):
        self.service.create_analysis(
            "combined",
            "crossovers",
            {"health": "health", "music": "music"},
            self.revision(),
        )

    def record_load(self, connection="music", instant=None):
        miroir = self.service.miroir()
        source = next(c for c in miroir.connections if c.config.id == connection)
        op = self.store.create_operation(
            miroir_id=miroir.id,
            connection_id=connection,
            integration=source.integration.name,
            kind="sync",
            origin="local",
            pipeline_name=source.binding.pipeline_name,
            dataset_name=source.binding.dataset_name,
            config_revision=source.revision,
            code_revision="test",
            backend_run_id=None,
        )
        attempt, _ = self.store.start_attempt(op.id, "running")
        self.store.update_attempt(
            attempt, ingestion="succeeded", transformation="skipped"
        )
        if instant:
            self.store.update_attempt(attempt, ingested_at=instant)
        return op

    def test_api_and_forms_create_edit_link_remove_and_preserve_connections(self):
        self.login()
        self.assertEqual(self.client.get("/analyses/new").status_code, 200)
        self.assertIn(
            "Health exports", self.client.get("/analyses/new?package=crossovers").text
        )
        original = copy.deepcopy(self.service._raw_config()[0]["connections"])
        response = self.client.post(
            "/analyses/new",
            data={
                "id": "combined",
                "package": "crossovers",
                "input.health": "health",
                "input.music": "",
                "input.biking": "",
                "file_revision": self.revision(),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Not linked: measures are unknown", response.text)
        stale = self.revision()
        response = self.client.put(
            "/api/analyses/combined",
            headers={**BEARER, "If-Match": stale},
            json={
                "inputs": {"health": "health", "music": "music"},
                "timezone": "America/Toronto",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.client.put(
                "/api/analyses/combined",
                headers={**BEARER, "If-Match": stale},
                json={"timezone": "UTC"},
            ).status_code,
            409,
        )
        for value in (None, [], 4):
            self.assertEqual(
                self.client.put(
                    "/api/analyses/combined",
                    headers={**BEARER, "If-Match": self.revision()},
                    json={"inputs": value},
                ).status_code,
                400,
            )
        self.assertEqual(
            self.client.delete("/api/connections/health", headers=BEARER).status_code,
            400,
        )
        removed = self.client.delete(
            "/api/analyses/combined", headers={**BEARER, "If-Match": self.revision()}
        )
        self.assertEqual(removed.status_code, 204)
        self.assertEqual(self.service._raw_config()[0]["connections"], original)

    def test_load_builds_each_linked_analysis_once_and_failure_does_not_stop_others(
        self,
    ):
        self.add_crossovers()
        plans = []

        def build(plan):
            plans.append(plan)
            if plan.title == "Music analysis":
                raise RuntimeError("synthetic music model failure")

        self.service.executor = InlineExecutor(
            lambda op: execute_operation(
                self.service.miroir(),
                op,
                store=self.store,
                destination=self.destination(),
                resolve_credentials=lambda c: {"access_token": "fake"},
                pipelines_dir=str(self.root / "state"),
                transform=build,
            )
        )
        operation_id = self.service.request_sync("music")
        self.assertEqual(len(plans), 2)
        builds = [
            o
            for o in self.store.recent_operations("personal", "music")
            if o["kind"] == "models"
        ]
        self.assertEqual({o["analysis_id"] for o in builds}, {"music", "combined"})
        self.assertTrue(all(o["parent_operation_id"] == operation_id for o in builds))
        self.assertEqual(self.store.attempts(operation_id)[0]["ingestion"], "succeeded")
        self.assertEqual(self.service.analysis("music")["status"]["state"], "stale")
        self.assertEqual(
            self.service.analysis("combined")["status"]["state"], "current"
        )
        self.assertEqual(self.service.analysis("health")["status"]["state"], "no_data")

    def destination(self):
        import dlt

        return dlt.destinations.duckdb(credentials=str(self.root / "w.duckdb"))

    def test_child_build_is_observed_through_parent_run_without_breaking_adoption(self):
        miroir = self.service.miroir()

        # Simulate the process disappearing during a child build.
        def crash(plan):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            run_connection(
                miroir,
                "music",
                store=self.store,
                origin="prefect",
                backend_run_id="parent-run",
                destination=self.destination(),
                resolve_credentials=lambda c: {"access_token": "fake"},
                pipelines_dir=str(self.root / "state"),
                transform=crash,
            )
        pending = self.store.unverified(miroir.id)
        self.assertEqual(len(pending), 2)
        self.assertEqual({p["backend_run_id"] for p in pending}, {"parent-run"})
        parent = self.store.find_by_backend_run("parent-run")
        self.assertEqual(parent.kind, "sync")
        for record in pending:
            self.store.close_unverified(
                record["operation_id"], record["attempt_id"], "lost", "crashed"
            )
        self.assertEqual(
            self.service.analysis("music")["status"]["last_outcome"], "lost"
        )
        self.assertIsNotNone(
            self.store.connection_status(miroir.id, "music")["last_ingestion_success"]
        )
        self.assertEqual(self.store.unverified(miroir.id), [])

    def test_freshness_uses_all_inputs_and_loads_during_build_leave_it_stale(self):
        self.add_crossovers()
        self.record_load()
        self.service.executor = InlineExecutor(
            lambda op: execute_operation(
                self.service.miroir(),
                op,
                store=self.store,
                destination="unused",
                resolve_credentials=lambda c: self.fail("rebuild resolved credentials"),
                transform=lambda plan: None,
            )
        )
        self.service.request_models("combined")
        self.assertEqual(
            self.service.analysis("combined")["status"]["state"], "current"
        )
        self.record_load("health")
        self.assertEqual(self.service.analysis("combined")["status"]["state"], "stale")
        self.service.request_models("combined")
        self.assertEqual(
            self.service.analysis("combined")["status"]["state"], "current"
        )

        def load_during_build(plan):
            self.record_load("health")

        self.service.executor = InlineExecutor(
            lambda op: execute_operation(
                self.service.miroir(),
                op,
                store=self.store,
                destination="unused",
                resolve_credentials=lambda c: {},
                transform=load_during_build,
            )
        )
        self.service.request_models("combined")
        self.assertEqual(self.service.analysis("combined")["status"]["state"], "stale")

    def test_build_outcomes_follow_completion_order_across_inputs(self):
        self.add_crossovers()
        self.record_load()
        self.service.executor = InlineExecutor(lambda _: None)
        older = self.service.request_models("combined")
        newer = self.service.request_models("combined")
        first, _ = self.store.start_attempt(older, "not_run")
        second, _ = self.store.start_attempt(newer, "not_run")
        self.store.update_attempt(first, transformation="running")
        self.store.update_attempt(second, transformation="running")
        receipt = analysis_plan(self.service.miroir().analyses[-1]).receipt
        self.store.update_attempt(
            second, transformation="succeeded", transformation_detail=receipt
        )
        self.assertEqual(
            self.service.analysis("combined")["status"]["state"], "running"
        )
        # The older request can acquire the dbt slot after the newer request.
        # Its later failure may leave partially replaced tables, so it must win.
        self.store.update_attempt(
            first, transformation="failed", transformation_detail="late failure"
        )
        self.assertEqual(self.service.analysis("combined")["status"]["state"], "stale")
        self.assertEqual(
            self.service.analysis("combined")["status"]["last_detail"], "late failure"
        )
        retry, _ = self.store.start_attempt(older, "not_run")
        self.store.update_attempt(
            retry, transformation="succeeded", transformation_detail=receipt
        )
        self.assertEqual(
            self.service.analysis("combined")["status"]["state"], "current"
        )

    def test_cli_lists_analyses_with_linked_inputs(self):
        import io
        from contextlib import redirect_stdout

        from src.cli.main import main

        self.add_crossovers()
        with redirect_stdout(io.StringIO()) as output:
            result = main(
                [
                    "--config",
                    str(self.config_path),
                    "analyses",
                    "--metadata",
                    str(self.store.url),
                ]
            )
        self.assertEqual(result, 0)
        combined = next(
            a for a in json.loads(output.getvalue()) if a["id"] == "combined"
        )
        self.assertEqual(combined["inputs"]["health"]["connection"], "health")
        self.assertEqual(combined["status"]["state"], "no_data")

    def test_schema_5_upgrade_backfills_existing_receipts(self):
        op = self.record_load()
        instant = datetime.now(timezone.utc)
        plan = analysis_plan(self.service.miroir().analyses[0])
        with self.store.engine.begin() as db:
            db.execute(
                attempts.update()
                .where(attempts.c.operation_id == op.id)
                .values(
                    transformation="succeeded",
                    transformation_detail=plan.receipt,
                    transformed_at=instant,
                    started_at=instant - timedelta(seconds=1),
                    ingested_at=instant - timedelta(seconds=2),
                )
            )
            db.execute(sa.text("ALTER TABLE operations DROP COLUMN analysis_id"))
            db.execute(
                sa.text("ALTER TABLE attempts DROP COLUMN transformation_updated_at")
            )
            db.execute(schema_info.update().values(version=5))
        self.store.migrate()
        self.assertEqual(self.store.get_operation(op.id).analysis_id, "music")
        self.assertEqual(self.service.analysis("music")["status"]["state"], "current")
        self.store.migrate()
        self.assertEqual(self.service.analysis("music")["status"]["state"], "current")
