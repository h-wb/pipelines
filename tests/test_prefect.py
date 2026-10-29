"""The Prefect adapter against a real, temporary Prefect API (prefect_test_harness).

No worker runs: flow runs stay scheduled unless a test sets their state.
"""

import io
import json
import os
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from src.adapters.prefect import PrefectOrchestrator
from src.cli.main import main
from src.core.config import ConfigurationError, parse_miroir
from src.core.operations import MetadataStore
from src.core.runner import request_sync
from tests.test_miroir import example

POOL = "test-pool"


def miroir(schedule=None, drop_health=False):
    config = example()
    if schedule:
        config["connections"][0]["schedule"] = schedule
    if drop_health:
        config["connections"] = config["connections"][:1]
    return parse_miroir(json.dumps(config))


class PrefectAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from prefect.client.schemas.actions import WorkPoolCreate
        from prefect.testing.utilities import prefect_test_harness

        cls.harness = prefect_test_harness()
        cls.harness.__enter__()
        from prefect import get_client

        with get_client(sync_client=True) as client:
            client.create_work_pool(WorkPoolCreate(name=POOL, type="process"))

    @classmethod
    def tearDownClass(cls):
        cls.harness.__exit__(None, None, None)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MetadataStore(f"sqlite:///{Path(self.tmp.name) / 'meta.sqlite'}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.orchestrator = PrefectOrchestrator(
            self.store,
            POOL,
            "/miroir/miroir.json",
            image="ghcr.io/x/pipelines:abc",
        )

    def converge(self, ws):
        self.orchestrator.apply(ws, self.orchestrator.plan(ws))
        self.assertEqual(self.orchestrator.plan(ws), [])

    def test_plan_apply_converges_and_reports_drift(self):
        daily = {"cron": "0 3 * * *", "timezone": "Europe/Paris"}
        ws = miroir(schedule=daily)
        plan = self.orchestrator.plan(ws)
        self.assertEqual(
            sorted((c["action"], c["deployment"]) for c in plan),
            [("create", "personal-health"), ("create", "personal-music")],
        )
        self.converge(ws)
        actual = self.orchestrator.actual(ws)
        self.assertEqual(
            actual["personal-music"]["comparable"]["schedule"],
            ("0 3 * * *", "Europe/Paris"),
        )
        self.assertEqual(
            actual["personal-music"]["comparable"]["image"], "ghcr.io/x/pipelines:abc"
        )

        # A schedule change in the miroir is an update...
        hourly = miroir(schedule={"cron": "0 * * * *", "timezone": "Europe/Paris"})
        [change] = self.orchestrator.plan(hourly)
        self.assertEqual(
            (change["action"], list(change["changes"])), ("update", ["schedule"])
        )
        self.converge(hourly)

        # ...a hand edit in Prefect is drift...
        from prefect import get_client
        from prefect.client.schemas.actions import DeploymentUpdate

        with get_client(sync_client=True) as client:
            client.update_deployment(
                uuid.UUID(actual["personal-health"]["id"]),
                DeploymentUpdate(
                    parameters={"connection_id": "health", "config_path": "/x"}
                ),
            )
        [drift] = self.orchestrator.plan(hourly)
        self.assertEqual(
            (drift["deployment"], list(drift["changes"])),
            ("personal-health", ["parameters"]),
        )
        self.converge(hourly)

        # ...and a removed connection is a delete.
        removed = miroir(
            schedule={"cron": "0 * * * *", "timezone": "Europe/Paris"}, drop_health=True
        )
        [delete] = self.orchestrator.plan(removed)
        self.assertEqual(
            (delete["action"], delete["deployment"]), ("delete", "personal-health")
        )
        self.converge(removed)

    def test_submit_is_idempotent_and_observe_reconciles(self):
        from prefect import get_client
        from prefect.states import Cancelled, Crashed

        ws = miroir()
        self.converge(ws)
        crashed = request_sync(ws, "music", store=self.store, origin="request")
        self.orchestrator.submit(crashed.id)
        first = self.store.get_operation(crashed.id).backend_run_id
        self.orchestrator.submit(crashed.id)  # e.g. retried after a timeout
        self.assertEqual(self.store.get_operation(crashed.id).backend_run_id, first)

        cancelled = request_sync(ws, "music", store=self.store, origin="request")
        self.orchestrator.submit(cancelled.id)
        running = request_sync(ws, "music", store=self.store, origin="request")
        self.orchestrator.submit(running.id)
        # The worker started this one and recorded a running attempt.
        self.store.start_attempt(running.id, "running")

        # Nothing has ended yet.
        self.assertEqual(self.orchestrator.observe(ws), [])
        with get_client(sync_client=True) as client:
            client.set_flow_run_state(uuid.UUID(first), Crashed(), force=True)
            client.set_flow_run_state(
                uuid.UUID(self.store.get_operation(cancelled.id).backend_run_id),
                Cancelled(),
                force=True,
            )
            client.set_flow_run_state(
                uuid.UUID(self.store.get_operation(running.id).backend_run_id),
                Crashed(),
                force=True,
            )
        closed = {
            item["operation_id"]: item["outcome"]
            for item in self.orchestrator.observe(ws)
        }
        self.assertEqual(
            closed, {crashed.id: "lost", cancelled.id: "cancelled", running.id: "lost"}
        )
        self.assertEqual(self.store.attempts(running.id)[0]["ingestion"], "lost")
        self.assertEqual(self.orchestrator.observe(ws), [])  # already closed

    def test_schedules_are_validated(self):
        for bad in (
            {"cron": "every day"},
            {"cron": "0 3 * * *", "timezone": "Mars/Base"},
        ):
            with self.subTest(bad=bad), self.assertRaises(ConfigurationError):
                miroir(schedule=bad)


class OrchestrationCliTests(unittest.TestCase):
    """The orchestrator commands as a person runs them: plain environment."""

    def setUp(self):
        # A machine without Prefect configured must still get a clear refusal.
        saved = os.environ.pop("MIROIR_PREFECT_POOL", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "MIROIR_PREFECT_POOL", saved)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        config = example()
        for connection in config["connections"]:
            if connection["integration"] == "apple_health":
                connection["settings"]["data_dir"] = str(root)  # readable
        self.config = root / "miroir.json"
        self.config.write_text(json.dumps(config))
        self.base = ["--config", str(self.config)]
        self.metadata = ["--metadata", f"sqlite:///{root / 'meta.sqlite'}"]

    def test_orchestrator_commands_refuse_without_a_pool(self):
        for command in (["deployments", "plan"], ["deployments", "apply"], ["refresh"]):
            with self.subTest(command=command):
                with redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(main([*self.base, *command, *self.metadata]), 2)
                self.assertIn("MIROIR_PREFECT_POOL", err.getvalue())

    def test_credential_commands_work_without_the_orchestrator(self):
        # Regression: the orchestrator wiring once moved `check`'s import away.
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main([*self.base, "check", "health", *self.metadata]), 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
