"""The Prefect adapter against a real, temporary Prefect API (prefect_test_harness).

With MIROIR_TEST_PREFECT_API_URL set, the adapter tests run against that server
instead: the Prefect contract job starts one of another version in a process of
its own (the Prefect version contract, docs/decisions).

No worker runs: flow runs stay scheduled unless a test sets their state.
"""

import io
import json
import os
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.adapters import prefect as adapter
from src.adapters.prefect import (
    MEDIA_TAG,
    PrefectOrchestrator,
    incompatibility,
    media_deployment_name,
)
from src.cli.main import main
from src.control.config import ConfigurationError, load_miroir, parse_miroir
from src.control.operations import MetadataStore, OrchestratorUnavailable
from src.control.runner import request_sync
from src.control.service import MiroirService
from src.kernel.errors import OperationError
from tests.test_miroir import example

POOL = "test-pool"
DEPLOYMENT = {"work_pool": POOL, "image": "ghcr.io/x/miroir:abc"}
# What the installation sets: a reference the work pool's template resolves.
MEDIA_JOB_VARIABLES = {"env_from": [{"secretRef": {"name": "miroir-media"}}]}
SCREEN = {"id": "screen", "integration": "screenmemory", "settings": {}}
# What `to_deployment` sets from the repository root, where the suite runs.
ENTRYPOINT = "src/flows/miroir.py:sync_connection"


def closed_api() -> str:
    """A Prefect API URL nothing listens on."""
    import socket

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    return f"http://127.0.0.1:{port}/api"


def miroir(schedule=None, drop_health=False, deployment=DEPLOYMENT):
    config = example()
    if deployment:
        config["deployment"] = deployment
    if schedule:
        config["connections"][0]["schedule"] = schedule
    if drop_health:
        config["connections"] = config["connections"][:1]
    return parse_miroir(json.dumps(config))


def with_screen(miroir_id, deployment=DEPLOYMENT):
    """The example plus a connection whose kind has thumbnails, as raw JSON."""
    raw = example()
    raw["id"] = miroir_id
    raw["deployment"] = dict(deployment)
    raw["connections"].append(dict(SCREEN))
    return raw


class PrefectAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from prefect.client.schemas.actions import WorkPoolCreate
        from prefect.exceptions import ObjectAlreadyExists

        external = os.environ.get("MIROIR_TEST_PREFECT_API_URL")
        if external:
            from prefect.settings import PREFECT_API_URL, temporary_settings

            cls.harness = temporary_settings({PREFECT_API_URL: external})
        else:
            from prefect.testing.utilities import prefect_test_harness

            cls.harness = prefect_test_harness()
        cls.harness.__enter__()
        from prefect import get_client

        with get_client(sync_client=True) as client:
            try:
                client.create_work_pool(WorkPoolCreate(name=POOL, type="process"))
            except ObjectAlreadyExists:  # an external server used before
                pass

    @classmethod
    def tearDownClass(cls):
        cls.harness.__exit__(None, None, None)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MetadataStore(f"sqlite:///{Path(self.tmp.name) / 'meta.sqlite'}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.orchestrator = PrefectOrchestrator(self.store, "/miroir/miroir.json")

    def delete_deployments(self, ws):
        from prefect import get_client

        with get_client(sync_client=True) as client:
            for deployment in self.orchestrator.actual(ws).values():
                client.delete_deployment(uuid.UUID(deployment["id"]))

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
        music = next(c["set"] for c in plan if c["deployment"] == "personal-music")
        self.assertEqual(
            json.loads(json.dumps(music)),
            {
                "entrypoint": ENTRYPOINT,
                "parameters": {
                    "connection_id": "music",
                    "config_path": "/miroir/miroir.json",
                },
                "schedules": [
                    {"cron": "0 3 * * *", "timezone": "Europe/Paris", "day_or": True}
                ],
                "work_pool_name": POOL,
                "job_variables": {"image": "ghcr.io/x/miroir:abc"},
                "concurrency_limit": 1,
            },
        )
        self.converge(ws)
        actual = self.orchestrator.actual(ws)
        [schedule] = actual["personal-music"]["attributes"]["schedules"]
        self.assertEqual(
            (schedule.cron, schedule.timezone), ("0 3 * * *", "Europe/Paris")
        )
        self.assertEqual(
            actual["personal-music"]["attributes"]["job_variables"],
            {"image": "ghcr.io/x/miroir:abc"},
        )

        # A schedule change in the miroir is an update...
        hourly = miroir(schedule={"cron": "0 * * * *", "timezone": "Europe/Paris"})
        [change] = self.orchestrator.plan(hourly)
        self.assertEqual(
            (change["action"], list(change["changes"])), ("update", ["schedules"])
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

        # ...as are a raised concurrency limit and hand-added non-cron schedules.
        from prefect.client.schemas.schedules import IntervalSchedule

        interval = [(IntervalSchedule(interval=timedelta(hours=1)), True)]
        with get_client(sync_client=True) as client:
            health = uuid.UUID(actual["personal-health"]["id"])
            client.update_deployment(health, DeploymentUpdate(concurrency_limit=5))
            client.create_deployment_schedules(health, interval)
            music = uuid.UUID(actual["personal-music"]["id"])
            client.create_deployment_schedules(music, interval)
        drift = {c["deployment"]: c["changes"] for c in self.orchestrator.plan(hourly)}
        self.assertEqual(
            {name: sorted(changes) for name, changes in drift.items()},
            {
                "personal-health": ["concurrency_limit", "schedules"],
                "personal-music": ["schedules"],
            },
        )
        self.assertEqual(
            drift["personal-health"]["concurrency_limit"], {"have": 5, "want": 1}
        )
        self.converge(hourly)

        # Prefect updates slugged schedules in place; apply still removes them.
        with get_client(sync_client=True) as client:
            client._client.post(
                f"/deployments/{health}/schedules",
                json=[{"schedule": {"interval": 3600}, "active": True, "slug": "hand"}],
            ).raise_for_status()
        self.assertEqual(len(self.orchestrator.plan(hourly)), 1)
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

    def test_setup_applies_only_its_connection_and_does_not_prune_other_deployments(
        self,
    ):
        raw = example()
        raw["id"] = "setup_scope"
        raw["deployment"] = DEPLOYMENT
        orphan = json.loads(json.dumps(raw["connections"][1]))
        orphan["id"] = "orphan"
        raw["connections"].append(orphan)
        self.converge(parse_miroir(json.dumps(raw)))
        raw["connections"].pop()
        raw["deployment"] = {"work_pool": POOL, "image": "ghcr.io/x/miroir:new"}
        path = Path(self.tmp.name) / "miroir.json"
        path.write_text(json.dumps(raw))
        service = MiroirService(
            path,
            self.store,
            Path(self.tmp.name) / "artifacts",
            self.orchestrator,
            orchestrator=self.orchestrator,
        )
        service.prepare_connection("health")
        actual = self.orchestrator.actual(service.miroir())
        self.assertEqual(
            actual["setup_scope-health"]["attributes"]["job_variables"]["image"],
            "ghcr.io/x/miroir:new",
        )
        self.assertEqual(
            actual["setup_scope-music"]["attributes"]["job_variables"]["image"],
            "ghcr.io/x/miroir:abc",
        )
        self.assertIn("setup_scope-orphan", actual)
        remaining = self.orchestrator.plan(service.miroir())
        self.assertEqual(
            {(c["action"], c["deployment"]) for c in remaining},
            {("update", "setup_scope-music"), ("delete", "setup_scope-orphan")},
        )

    def test_a_deployment_at_the_former_entrypoint_is_an_update(self):
        from prefect import get_client
        from prefect.client.schemas.actions import DeploymentUpdate

        raw = example()
        raw["id"] = "moved_flow"
        raw["deployment"] = DEPLOYMENT
        ws = parse_miroir(json.dumps(raw))
        self.addCleanup(self.delete_deployments, ws)
        self.converge(ws)
        actual = self.orchestrator.actual(ws)
        self.assertEqual(
            {name: d["attributes"]["entrypoint"] for name, d in actual.items()},
            {"moved_flow-health": ENTRYPOINT, "moved_flow-music": ENTRYPOINT},
        )

        # A deployment applied before the flow's package was renamed.
        former = "src/pipelines/miroir.py:sync_connection"
        with get_client(sync_client=True) as client:
            client.update_deployment(
                uuid.UUID(actual["moved_flow-health"]["id"]),
                DeploymentUpdate(entrypoint=former),
            )
        [update] = self.orchestrator.plan(ws)
        self.assertEqual(
            (update["action"], update["deployment"], update["changes"]),
            (
                "update",
                "moved_flow-health",
                {"entrypoint": {"have": former, "want": ENTRYPOINT}},
            ),
        )
        self.converge(ws)
        self.assertEqual(
            self.orchestrator.actual(ws)["moved_flow-health"]["attributes"][
                "entrypoint"
            ],
            ENTRYPOINT,
        )

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

        modelling = request_sync(ws, "music", store=self.store, origin="request")
        self.orchestrator.submit(modelling.id)
        model_attempt, _ = self.store.start_attempt(modelling.id, "running")
        # The load committed, but the process disappears before/during dbt.
        self.store.update_attempt(model_attempt, ingestion="succeeded")

        rebuilding = request_sync(ws, "music", store=self.store, origin="request")
        self.orchestrator.submit(rebuilding.id)
        self.store.start_attempt(rebuilding.id, "not_run")

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
            for operation in (modelling, rebuilding):
                client.set_flow_run_state(
                    uuid.UUID(self.store.get_operation(operation.id).backend_run_id),
                    Crashed(),
                    force=True,
                )
        closed = {
            item["operation_id"]: item["outcome"]
            for item in self.orchestrator.observe(ws)
        }
        self.assertEqual(
            closed,
            {
                crashed.id: "lost",
                cancelled.id: "cancelled",
                running.id: "lost",
                modelling.id: "lost",
                rebuilding.id: "lost",
            },
        )
        self.assertEqual(self.store.attempts(running.id)[0]["ingestion"], "lost")
        self.assertEqual(self.store.attempts(modelling.id)[0]["ingestion"], "succeeded")
        self.assertEqual(self.store.attempts(modelling.id)[0]["transformation"], "lost")
        self.assertEqual(self.store.attempts(rebuilding.id)[0]["ingestion"], "not_run")
        self.assertEqual(
            self.store.attempts(rebuilding.id)[0]["transformation"], "lost"
        )
        self.assertEqual(self.orchestrator.observe(ws), [])  # already closed

    def test_a_deferred_build_is_a_scheduled_run_of_the_same_deployment(self):
        from prefect import get_client
        from prefect.client.schemas.objects import StateType

        from src.control.runner import request_models
        from src.flows.miroir import schedule_build

        ws = miroir()
        self.converge(ws)
        # Later tests expect a Prefect without this miroir's deployments.
        self.addCleanup(self.delete_deployments, ws)
        load = request_sync(ws, "music", store=self.store, origin="request")
        attempt, _ = self.store.start_attempt(load.id, "running")
        self.store.update_attempt(attempt, ingestion="succeeded")
        build = request_models(ws, "music", store=self.store, origin="prefect")
        deployment_id = self.orchestrator.actual(ws)["personal-music"]["id"]
        schedule = schedule_build(
            self.store, deployment_id, "music", "/miroir/miroir.json"
        )
        start_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
            minutes=5
        )
        schedule(build.id, start_at)
        bound = self.store.get_operation(build.id).backend_run_id
        schedule(build.id, start_at)  # e.g. retried: the same run
        self.assertEqual(self.store.get_operation(build.id).backend_run_id, bound)
        with get_client(sync_client=True) as client:
            run = client.read_flow_run(uuid.UUID(bound))
        self.assertEqual(str(run.deployment_id), deployment_id)
        self.assertEqual(run.state.type, StateType.SCHEDULED)
        self.assertEqual(run.expected_start_time, start_at)
        self.assertIsNone(run.parent_task_run_id)
        self.assertEqual(
            run.parameters,
            {
                "connection_id": "music",
                "operation_id": build.id,
                "config_path": "/miroir/miroir.json",
            },
        )
        # Scheduled, not ended: observing leaves it alone.
        self.assertEqual(self.orchestrator.observe(ws), [])

    def test_a_derive_run_is_scheduled_for_its_earliest_start(self):
        from prefect import get_client
        from prefect.client.schemas.objects import StateType

        from src.control.runner import _new_operation

        ws = parse_miroir(json.dumps(with_screen("personal")))
        self.converge(ws)
        self.addCleanup(self.delete_deployments, ws)
        start_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
            minutes=5
        )
        derive = _new_operation(
            self.store,
            ws,
            ws.connections[2],
            "webhook",
            kind="derive",
            options=json.dumps({"not_before": start_at.isoformat()}),
        )
        self.orchestrator.submit(derive.id)
        bound = self.store.get_operation(derive.id).backend_run_id
        with get_client(sync_client=True) as client:
            run = client.read_flow_run(uuid.UUID(bound))
        self.assertEqual(run.state.type, StateType.SCHEDULED)
        self.assertEqual(run.expected_start_time, start_at)
        self.assertEqual(run.parameters["operation_id"], derive.id)

    def test_whether_a_derive_run_is_still_waiting(self):
        from prefect import get_client
        from prefect.states import Failed

        from src.control.runner import _new_operation

        ws = parse_miroir(json.dumps(with_screen("personal")))
        self.converge(ws)
        self.addCleanup(self.delete_deployments, ws)
        runs = []
        for _ in range(3):
            derive = _new_operation(
                self.store, ws, ws.connections[2], "webhook", kind="derive"
            )
            self.orchestrator.submit(derive.id)
            runs.append(self.store.get_operation(derive.id).backend_run_id)
        scheduled, failed, deleted = runs
        with get_client(sync_client=True) as client:
            client.set_flow_run_state(uuid.UUID(failed), Failed(), force=True)
            client.delete_flow_run(uuid.UUID(deleted))
        self.assertTrue(self.orchestrator.waiting(scheduled))
        self.assertFalse(self.orchestrator.waiting(failed))
        self.assertFalse(self.orchestrator.waiting(deleted))

    def test_a_deployment_prefect_rejects_on_create_is_a_readable_error(self):
        raw = example()
        raw["id"] = "rejected"
        raw["deployment"] = {"work_pool": "missing-pool"}
        ws = parse_miroir(json.dumps(raw))
        with self.assertRaisesRegex(
            OperationError, 'Work pool "missing-pool" not found'
        ):
            self.orchestrator.apply(ws, self.orchestrator.plan(ws))

    def test_unreachable_or_refusing_prefect_is_unavailable(self):
        import httpx
        from prefect.settings import PREFECT_API_URL, temporary_settings

        ws = miroir()
        operation = request_sync(ws, "music", store=self.store, origin="request")
        with temporary_settings({PREFECT_API_URL: closed_api()}):
            for call in (
                lambda: self.orchestrator.submit(operation.id),
                lambda: self.orchestrator.plan(ws),
            ):
                with self.assertRaisesRegex(OrchestratorUnavailable, "try again"):
                    call()
            self.store.bind_backend_run(operation.id, str(uuid.uuid4()))
            self.store.start_attempt(operation.id, "running")
            with self.assertRaisesRegex(OrchestratorUnavailable, "try again"):
                self.orchestrator.observe(ws)
        request = httpx.Request("POST", "http://prefect/api")
        refused = httpx.HTTPStatusError(
            "refused", request=request, response=httpx.Response(401, request=request)
        )
        with (
            patch("prefect.deployments.run_deployment", side_effect=refused),
            self.assertRaisesRegex(OrchestratorUnavailable, "401"),
        ):
            self.orchestrator.submit(operation.id)

    def test_an_older_prefect_server_is_refused_before_any_call(self):
        import prefect
        from prefect.client.orchestration import SyncPrefectClient

        major, minor = prefect.__version__.split(".")[:2]
        older = (
            f"{major}.{int(minor) - 1}.0" if int(minor) else f"{int(major) - 1}.99.0"
        )
        ws = miroir()
        operation = request_sync(ws, "music", store=self.store, origin="request")
        with (
            patch.object(adapter, "_compatible_servers", set()),
            patch.object(SyncPrefectClient, "api_version", return_value=older),
            patch("prefect.deployments.run_deployment") as run_deployment,
        ):
            for call in (
                lambda: self.orchestrator.submit(operation.id),
                lambda: self.orchestrator.plan(ws),
                lambda: self.orchestrator.observe(ws),
            ):
                with self.assertRaisesRegex(
                    OperationError, "Upgrade the Prefect server"
                ):
                    call()
            run_deployment.assert_not_called()

    def test_the_server_is_checked_once_per_process(self):
        from prefect.client.orchestration import SyncPrefectClient

        ws = miroir()
        with (
            patch.object(adapter, "_compatible_servers", set()),
            patch.object(
                SyncPrefectClient,
                "api_version",
                autospec=True,
                side_effect=SyncPrefectClient.api_version,
            ) as api_version,
        ):
            self.orchestrator.plan(ws)
            self.orchestrator.plan(ws)
        self.assertEqual(api_version.call_count, 1)

    def test_plan_and_apply_own_the_media_deployment(self):
        raw = with_screen("media_owned")
        media = media_deployment_name("media_owned")
        ws = parse_miroir(json.dumps(raw))
        self.addCleanup(self.delete_deployments, ws)
        # An installation from before the media deployment: its connections only.
        self.orchestrator.apply(
            ws,
            [c for c in self.orchestrator.plan(ws) if c["connection_id"] is not None],
        )
        before = self.orchestrator.actual(ws)
        self.assertNotIn(media, before)
        [create] = self.orchestrator.plan(ws)
        self.assertEqual(
            (create["action"], create["deployment"], create["connection_id"]),
            ("create", media, None),
        )
        self.assertEqual(
            json.loads(json.dumps(create["set"])),
            {
                "entrypoint": ENTRYPOINT,
                "parameters": {"config_path": "/miroir/miroir.json"},
                "schedules": [],
                "work_pool_name": POOL,
                "job_variables": {"image": "ghcr.io/x/miroir:abc"},
                "concurrency_limit": 1,
            },
        )
        self.converge(ws)
        after = self.orchestrator.actual(ws)
        self.assertEqual(
            {name: after[name] for name in before}, before
        )  # the connections' deployments are untouched

        # The media job variables are an update of the media deployment alone.
        raw["deployment"]["media_job_variables"] = MEDIA_JOB_VARIABLES
        referenced = parse_miroir(json.dumps(raw))
        [update] = self.orchestrator.plan(referenced)
        self.assertEqual(
            (update["action"], update["deployment"], list(update["changes"])),
            ("update", media, ["job_variables"]),
        )
        self.converge(referenced)
        actual = self.orchestrator.actual(referenced)
        self.assertEqual(
            actual[media]["attributes"]["job_variables"],
            {**MEDIA_JOB_VARIABLES, "image": "ghcr.io/x/miroir:abc"},
        )
        self.assertEqual(
            actual["media_owned-screen"]["attributes"]["job_variables"],
            {"image": "ghcr.io/x/miroir:abc"},
        )

        # Without a kind that has thumbnails, it is removed with its connection.
        raw["connections"].pop()
        removed = parse_miroir(json.dumps(raw))
        self.assertEqual(
            sorted(
                (c["action"], c["deployment"]) for c in self.orchestrator.plan(removed)
            ),
            [("delete", media), ("delete", "media_owned-screen")],
        )
        self.converge(removed)

    def test_derive_runs_go_to_the_media_deployment(self):
        from prefect import get_client

        from src.control.runner import _new_operation

        ws = parse_miroir(json.dumps(with_screen("routing")))
        self.converge(ws)
        self.addCleanup(self.delete_deployments, ws)
        actual = self.orchestrator.actual(ws)
        screen = ws.connections[2]
        derive = _new_operation(self.store, ws, screen, "webhook", kind="derive")
        load = _new_operation(self.store, ws, screen, "webhook", kind="sync")
        for operation in (derive, load):
            self.orchestrator.submit(operation.id)
        with get_client(sync_client=True) as client:
            runs = {
                operation.kind: client.read_flow_run(
                    uuid.UUID(self.store.get_operation(operation.id).backend_run_id)
                )
                for operation in (derive, load)
            }
        self.assertEqual(
            str(runs["derive"].deployment_id), actual["routing--media"]["id"]
        )
        self.assertEqual(
            str(runs["sync"].deployment_id), actual["routing-screen"]["id"]
        )
        for run in runs.values():
            self.assertEqual(run.parameters["connection_id"], "screen")
        self.assertEqual(runs["derive"].parameters["operation_id"], derive.id)

    def test_preparing_a_connection_also_prepares_the_media_deployment(self):
        raw = with_screen("prepared")
        path = Path(self.tmp.name) / "miroir.json"
        path.write_text(json.dumps(raw))
        service = MiroirService(
            path,
            self.store,
            Path(self.tmp.name) / "artifacts",
            self.orchestrator,
            orchestrator=self.orchestrator,
        )
        self.addCleanup(self.delete_deployments, service.miroir())
        service.prepare_connection("screen")
        self.assertEqual(
            sorted(self.orchestrator.actual(service.miroir())),
            ["prepared--media", "prepared-screen"],
        )

    def test_schedules_are_validated(self):
        for bad in (
            {"cron": "every day"},
            {"cron": "0 3 * * *", "timezone": "Mars/Base"},
        ):
            with self.subTest(bad=bad), self.assertRaises(ConfigurationError):
                miroir(schedule=bad)


class ServerVersionTests(unittest.TestCase):
    def test_a_server_at_or_after_the_clients_minor_is_compatible(self):
        for server in ("3.9.0", "3.9.7", "3.10.0", "3.12.1.dev4+g1a2b3c"):
            with self.subTest(server=server):
                self.assertIsNone(incompatibility("3.9.2", server))

    def test_an_older_minor_or_another_major_is_not(self):
        self.assertRegex(
            incompatibility("3.9.0", "3.8.8"), "Upgrade .* to 3.9 or later"
        )
        self.assertRegex(incompatibility("3.9.0", "4.0.0"), "share a major version")
        self.assertRegex(incompatibility("3.9.0", "2.20.0"), "share a major version")

    def test_an_unreadable_version_is_a_product_error(self):
        with self.assertRaisesRegex(OperationError, "unreadable"):
            incompatibility("3.9.0", "nightly")


class DeploymentSettingsTests(unittest.TestCase):
    """The configuration is the single source of execution settings (016)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MetadataStore(f"sqlite:///{Path(self.tmp.name) / 'meta.sqlite'}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()

    def test_the_deployment_section_maps_onto_prefect_attributes(self):
        declared = miroir(
            deployment={
                "work_pool": "config-pool",
                "image": "ghcr.io/h-wb/miroir:sha-config",
                "job_variables": {"namespace": "from-config"},
            }
        )
        adapter = PrefectOrchestrator(self.store, "/config.json")
        spec = next(iter(adapter.desired(declared).values()))
        self.assertEqual(spec["work_pool_name"], "config-pool")
        self.assertEqual(
            spec["job_variables"],
            {"namespace": "from-config", "image": "ghcr.io/h-wb/miroir:sha-config"},
        )
        bare = miroir(deployment={"work_pool": "config-pool"})
        self.assertEqual(
            next(iter(adapter.desired(bare).values()))["job_variables"], {}
        )

    def test_the_media_deployment_has_its_own_job_variables(self):
        adapter = PrefectOrchestrator(self.store, "/config.json")
        deployment = {
            "work_pool": "config-pool",
            "image": "ghcr.io/h-wb/miroir:sha-config",
            "job_variables": {"namespace": "from-config"},
        }
        plain = adapter.desired(parse_miroir(json.dumps(with_screen("m", deployment))))
        specs = adapter.desired(
            parse_miroir(
                json.dumps(
                    with_screen(
                        "m", {**deployment, "media_job_variables": MEDIA_JOB_VARIABLES}
                    )
                )
            )
        )
        media = specs.pop("m--media")
        # The connections' deployments do not change.
        self.assertEqual(
            {name: spec for name, spec in plain.items() if name != "m--media"}, specs
        )
        self.assertEqual(sorted(specs), ["m-health", "m-music", "m-screen"])
        self.assertEqual(
            media["job_variables"],
            {
                "namespace": "from-config",
                **MEDIA_JOB_VARIABLES,
                "image": "ghcr.io/h-wb/miroir:sha-config",
            },
        )
        self.assertEqual(media["parameters"], {"config_path": "/config.json"})
        self.assertEqual(
            (media["work_pool_name"], media["schedules"], media["concurrency_limit"]),
            ("config-pool", [], 1),
        )
        self.assertEqual(media["tags"], ["miroir", "miroir:m", MEDIA_TAG])
        # Without media job variables it runs like the others.
        self.assertEqual(
            plain["m--media"]["job_variables"], specs["m-screen"]["job_variables"]
        )
        # The image is the deployment's, whatever the media job variables say.
        overridden = with_screen(
            "m", {**deployment, "media_job_variables": {"image": "other"}}
        )
        self.assertEqual(
            adapter.desired(parse_miroir(json.dumps(overridden)))["m--media"][
                "job_variables"
            ]["image"],
            "ghcr.io/h-wb/miroir:sha-config",
        )
        # Without deployment.image too: the connections' image from job_variables.
        unpinned = {key: value for key, value in deployment.items() if key != "image"}
        unpinned = with_screen(
            "m",
            {
                **unpinned,
                "job_variables": {"image": "from-job-variables"},
                "media_job_variables": {"image": "other"},
            },
        )
        self.assertEqual(
            adapter.desired(parse_miroir(json.dumps(unpinned)))["m--media"][
                "job_variables"
            ]["image"],
            "from-job-variables",
        )
        # No kind with thumbnails, no media deployment.
        self.assertEqual(
            sorted(adapter.desired(miroir())), ["personal-health", "personal-music"]
        )

    def test_the_media_deployment_name_is_no_connections(self):
        name = media_deployment_name("personal")
        adapter = PrefectOrchestrator(self.store, "/config.json")
        specs = adapter.desired(parse_miroir(json.dumps(with_screen("personal"))))
        self.assertIn(name, specs)
        self.assertEqual(
            [spec["parameters"].get("connection_id") for spec in specs.values()].count(
                None
            ),
            1,
        )
        # `<miroir>-<connection>` would equal it only for the connection `-media`,
        # which is not an identifier.
        raw = with_screen("personal")
        raw["connections"][2]["id"] = "-media"
        with self.assertRaises(ConfigurationError):
            parse_miroir(json.dumps(raw))

    def test_no_deployment_section_is_a_clear_error(self):
        adapter = PrefectOrchestrator(self.store, "/config.json")
        with self.assertRaisesRegex(OperationError, "deployment.work_pool"):
            adapter.desired(miroir(deployment=None))

    def test_deployment_settings_do_not_change_connection_revisions(self):
        before = miroir(deployment=None)
        after = miroir(
            deployment={"work_pool": "other-pool", "image": "ghcr.io/x:other"}
        )
        self.assertEqual(
            [c.revision for c in before.connections],
            [c.revision for c in after.connections],
        )


class OrchestrationCliTests(unittest.TestCase):
    """The orchestrator commands as a person runs them: plain environment."""

    def setUp(self):
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
                self.assertIn("deployment.work_pool", err.getvalue())

    def test_orchestrator_command_errors_are_messages_not_tracebacks(self):
        from prefect.settings import PREFECT_API_URL, temporary_settings

        store = MetadataStore(self.metadata[1])
        self.addCleanup(store.engine.dispose)
        store.migrate()
        config = json.loads(self.config.read_text())
        config["deployment"] = {"work_pool": POOL}
        self.config.write_text(json.dumps(config))
        ws = load_miroir(self.config)
        unverified = request_sync(ws, "music", store=store, origin="request")
        store.bind_backend_run(unverified.id, str(uuid.uuid4()))
        store.start_attempt(unverified.id, "running")
        with temporary_settings({PREFECT_API_URL: closed_api()}):
            for command, message in (
                (["models", "nope"], "Unknown analysis"),
                (["models", "nope", "--full-refresh"], "Unknown analysis"),
                (["deployments", "plan"], "try again"),
                (["deployments", "apply"], "try again"),
                (["refresh"], "try again"),
            ):
                with self.subTest(command=command):
                    with redirect_stderr(io.StringIO()) as err:
                        code = main([*self.base, *command, *self.metadata])
                    self.assertEqual(code, 1)
                    self.assertIn(message, err.getvalue())

    def test_credential_commands_work_without_the_orchestrator(self):
        # Regression: the orchestrator wiring once moved `check`'s import away.
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main([*self.base, "check", "health", *self.metadata]), 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
