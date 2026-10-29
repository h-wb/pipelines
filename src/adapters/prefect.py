"""Prefect as the miroir's orchestrator (decision 013).

The miroir owns desired state (connections, schedules); Prefect owns execution.
This adapter only translates: deployments from configuration (plan/apply),
requests into flow runs (submit), and flow run states back into attempts (observe).
"""

import inspect
import uuid
from dataclasses import dataclass, field
from typing import Any

from src.core.config import ResolvedConnection, ResolvedMiroir
from src.core.operations import MetadataStore

FLOW_NAME = "sync-connection"


def _sync(value: Any) -> Any:
    """Prefect methods return results in sync contexts and coroutines in async ones."""
    if inspect.iscoroutine(value):
        from prefect.utilities.asyncutils import run_coro_as_sync

        return run_coro_as_sync(value)
    return value


@dataclass(frozen=True)
class DeploymentSpec:
    """What Prefect should have for one connection. Compared field by field."""

    name: str
    connection_id: str
    parameters: dict[str, Any]
    cron: str | None
    timezone: str | None
    work_pool: str
    image: str | None
    job_variables: dict[str, Any]

    def comparable(self) -> dict[str, Any]:
        return {
            "parameters": self.parameters,
            "schedule": (self.cron, self.timezone) if self.cron else None,
            "work_pool": self.work_pool,
            "image": self.image,
            "job_variables": self.job_variables,
        }


@dataclass
class PrefectOrchestrator:
    store: MetadataStore
    work_pool: str
    # Path of the miroir configuration where flow runs execute (worker or Job).
    config_path: str
    image: str | None = None
    # Extra job variables (e.g. `working_dir` for a process pool).
    job_variables: dict[str, Any] = field(default_factory=dict)

    # ---- Deployments ------------------------------------------------------------

    def deployment_name(
        self, miroir: ResolvedMiroir, connection: ResolvedConnection
    ) -> str:
        return f"{miroir.id}-{connection.config.id}"

    def desired(self, miroir: ResolvedMiroir) -> dict[str, DeploymentSpec]:
        specs = {}
        for connection in miroir.connections:
            schedule = connection.config.schedule
            name = self.deployment_name(miroir, connection)
            specs[name] = DeploymentSpec(
                name=name,
                connection_id=connection.config.id,
                parameters={
                    "connection_id": connection.config.id,
                    "config_path": self.config_path,
                },
                cron=schedule.cron if schedule else None,
                timezone=schedule.timezone if schedule else None,
                work_pool=self.work_pool,
                image=self.image,
                job_variables=dict(self.job_variables),
            )
        return specs

    def _tag(self, miroir: ResolvedMiroir) -> str:
        return f"miroir:{miroir.id}"

    def actual(self, miroir: ResolvedMiroir) -> dict[str, dict[str, Any]]:
        from prefect import get_client
        from prefect.client.schemas.filters import (
            DeploymentFilter,
            DeploymentFilterTags,
            FlowFilter,
            FlowFilterName,
        )
        from prefect.client.schemas.schedules import CronSchedule

        with get_client(sync_client=True) as client:
            deployments = client.read_deployments(
                flow_filter=FlowFilter(name=FlowFilterName(any_=[FLOW_NAME])),
                deployment_filter=DeploymentFilter(
                    tags=DeploymentFilterTags(all_=[self._tag(miroir)])
                ),
            )
        result = {}
        for deployment in deployments:
            crons = [
                (s.schedule.cron, s.schedule.timezone)
                for s in deployment.schedules
                if isinstance(s.schedule, CronSchedule)
            ]
            variables = dict(deployment.job_variables or {})
            image = variables.pop("image", None)
            result[deployment.name] = {
                "id": str(deployment.id),
                "comparable": {
                    "parameters": dict(deployment.parameters or {}),
                    "schedule": crons[0] if len(crons) == 1 else (crons or None),
                    "work_pool": deployment.work_pool_name,
                    "image": image,
                    "job_variables": variables,
                },
            }
        return result

    def plan(self, miroir: ResolvedMiroir) -> list[dict[str, Any]]:
        """Changes needed for Prefect to match the miroir. Read-only."""
        desired, actual = self.desired(miroir), self.actual(miroir)
        changes: list[dict[str, Any]] = []
        for name, spec in sorted(desired.items()):
            want = spec.comparable()
            if name not in actual:
                changes.append({"action": "create", "deployment": name, "set": want})
                continue
            have = actual[name]["comparable"]
            diff = {
                key: {"have": have[key], "want": want[key]}
                for key in want
                if have[key] != want[key]
            }
            if diff:
                changes.append(
                    {"action": "update", "deployment": name, "changes": diff}
                )
        for name in sorted(set(actual) - set(desired)):
            changes.append(
                {"action": "delete", "deployment": name, "id": actual[name]["id"]}
            )
        return changes

    def apply(self, miroir: ResolvedMiroir, changes: list[dict[str, Any]]) -> None:
        """Perform a plan. Create and update are the same upsert in Prefect."""
        from prefect import get_client
        from prefect.client.schemas.schedules import CronSchedule

        from src.pipelines.miroir import sync_connection

        desired = self.desired(miroir)
        for change in changes:
            if change["action"] == "delete":
                with get_client(sync_client=True) as client:
                    client.delete_deployment(uuid.UUID(change["id"]))
                continue
            spec = desired[change["deployment"]]
            deployment = _sync(
                sync_connection.to_deployment(
                    name=spec.name,
                    parameters=spec.parameters,
                    schedules=[CronSchedule(cron=spec.cron, timezone=spec.timezone)]
                    if spec.cron
                    else [],
                    work_pool_name=spec.work_pool,
                    job_variables=spec.job_variables,
                    concurrency_limit=1,  # Prefect queues overlapping runs of a connection
                    tags=["miroir", self._tag(miroir)],
                )
            )
            _sync(deployment.apply(work_pool_name=spec.work_pool, image=spec.image))

    # ---- Execution --------------------------------------------------------------

    def submit(self, operation_id: str) -> None:
        """Start the connection's deployment for a requested operation.

        The operation ID is the idempotency key: resubmitting after a timeout or
        crash returns the existing flow run instead of starting another.
        """
        from prefect.deployments import run_deployment

        operation = self.store.get_operation(operation_id)
        name = f"{FLOW_NAME}/{operation.miroir_id}-{operation.connection_id}"
        flow_run = _sync(
            run_deployment(
                name,
                parameters={
                    "connection_id": operation.connection_id,
                    "operation_id": operation.id,
                    "config_path": self.config_path,
                },
                timeout=0,
                idempotency_key=operation.id,
            )
        )
        self.store.bind_backend_run(operation.id, str(flow_run.id))

    def observe(self, miroir: ResolvedMiroir) -> list[dict[str, Any]]:
        """Close attempts whose flow run ended without recording an outcome."""
        from prefect import get_client
        from prefect.client.schemas.objects import StateType
        from prefect.exceptions import ObjectNotFound

        closed = []
        with get_client(sync_client=True) as client:
            for item in self.store.unverified(miroir.id):
                try:
                    run = client.read_flow_run(uuid.UUID(item["backend_run_id"]))
                    state = run.state.type if run.state else None
                    label = run.state.name if run.state else "unknown"
                except ObjectNotFound:
                    state, label = StateType.CRASHED, "deleted"
                if state in (StateType.CANCELLED, StateType.CANCELLING):
                    outcome = "cancelled"
                elif state in (
                    StateType.CRASHED,
                    StateType.FAILED,
                    StateType.COMPLETED,
                ):
                    # Failed or completed while our attempt still says running: the
                    # run ended before it could record anything.
                    outcome = "lost"
                else:
                    continue  # scheduled, pending, running, paused: not ours to decide
                self.store.close_unverified(
                    item["operation_id"],
                    item["attempt_id"],
                    outcome,  # type: ignore[arg-type]
                    f"Prefect flow run ended as {label} without recording an outcome",
                )
                closed.append({**item, "outcome": outcome, "state": label})
        return closed
