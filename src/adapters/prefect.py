"""Prefect as the miroir's orchestrator (decision 013).

The miroir owns desired state (connections, schedules); Prefect owns execution.
This adapter only translates: deployments from configuration (plan/apply),
requests into flow runs (submit), and flow run states back into attempts (observe).

Each connection has its own deployment; derive runs (thumbnails, backfill walks)
of every connection share one media deployment, so the work pool's template can
give the bucket key to its Jobs alone (decision 033 amendment).

It blocks, so callers run it off the event loop (the web routes that reach it are
synchronous). On a loop Prefect's `to_deployment` and `run_deployment` would hand
back coroutines instead of results.

The locked Prefect client is the contract with the Prefect server: before its
first call to a server, a process checks that the server is at least the
client's minor version, on the same major ([the Prefect version
contract](../../docs/decisions/2026-11-27-prefect-version-contract.md)).
"""

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import Any, Literal, ParamSpec, TypedDict, TypeVar

from src.control.config import ResolvedMiroir
from src.control.operations import MetadataStore, OrchestratorUnavailable
from src.kernel.errors import OperationError

P = ParamSpec("P")
R = TypeVar("R")

FLOW_NAME = "sync-connection"
# Prefect queues overlapping runs of one connection, and derive runs of the miroir.
CONCURRENCY_LIMIT = 1
# Tags the media deployment, beside the miroir's own tags.
MEDIA_TAG = "miroir:media"
# The deployment attributes the miroir owns; a difference in any of them is drift.
# `entrypoint` is not one of `to_deployment`'s arguments: it is what it sets.
OWNED = (
    "entrypoint",
    "parameters",
    "schedules",
    "work_pool_name",
    "job_variables",
    "concurrency_limit",
)
UNAVAILABLE = (
    "Execution is unavailable; check the Prefect service and credentials,"
    " then try again."
)


# Prefect API URLs this process found compatible; each is checked once.
_compatible_servers: set[str] = set()


def _release(version: str) -> tuple[int, int]:
    """Major and minor of a Prefect version (`3.9.0`, `3.10.1.dev2+g1a2b`)."""
    match = re.match(r"(\d+)\.(\d+)", version)
    if match is None:
        raise OperationError(f"Prefect reported an unreadable version: {version!r}")
    return int(match.group(1)), int(match.group(2))


def incompatibility(client: str, server: str) -> str | None:
    """Why this client cannot use that server, or None when it can.

    The server must be on the client's major and at least its minor. Patch
    releases differ freely; Prefect itself only warns when the server is older.
    """
    have, need = _release(server), _release(client)
    if have[0] != need[0]:
        return (
            f"The Prefect server runs {server}, Miroir's Prefect client {client}:"
            f" they must share a major version. Run a Prefect {need[0]}.x server"
            f" at {need[0]}.{need[1]} or later."
        )
    if have < need:
        return (
            f"The Prefect server runs {server}, older than Miroir's Prefect client"
            f" {client}. Upgrade the Prefect server to {need[0]}.{need[1]} or later."
        )
    return None


def _require_compatible_server() -> None:
    """Refuse a Prefect server older than the locked client, once per server."""
    import prefect
    from prefect import get_client
    from prefect.settings import get_current_settings

    url = str(get_current_settings().api.url)
    if url in _compatible_servers:
        return
    with get_client(sync_client=True) as client:
        server = client.api_version()
    problem = incompatibility(prefect.__version__, server)
    if problem is not None:
        raise OperationError(problem)
    _compatible_servers.add(url)


def _readable_errors(function: Callable[P, R]) -> Callable[P, R]:
    """Prefect failures as product errors; an unreachable or refusing Prefect is
    OrchestratorUnavailable, so callers can ask to try again later. Checks the
    server's version first."""

    @wraps(function)
    def call(*args: P.args, **kwargs: P.kwargs) -> R:
        import httpx
        from prefect.deployments.runner import DeploymentApplyError
        from prefect.exceptions import ObjectNotFound

        try:
            _require_compatible_server()
            return function(*args, **kwargs)
        except ObjectNotFound as error:
            raise OperationError(
                "Execution work pool is unavailable; check the runtime configuration and try again."
            ) from error
        except DeploymentApplyError as error:
            if isinstance(error.__cause__, httpx.TransportError):
                raise OrchestratorUnavailable(UNAVAILABLE) from error
            # Creating a deployment wraps Prefect's answer (e.g. a missing work pool).
            raise OperationError(f"Prefect rejected the deployment: {error}") from error
        except httpx.HTTPStatusError as error:
            raise OrchestratorUnavailable(
                f"Prefect refused the request ({error.response.status_code}): check"
                " the Prefect service and the server's credentials, then try again."
            ) from error
        except httpx.HTTPError as error:
            raise OrchestratorUnavailable(UNAVAILABLE) from error

    return call


class DeploymentSpec(TypedDict):
    """One connection's deployment, or the media deployment, in Prefect's own
    names: exactly the keyword arguments `Flow.to_deployment` takes (decision 029)."""

    name: str
    parameters: dict[str, Any]
    schedules: list[Any]  # Prefect schedule models
    work_pool_name: str
    # Includes `image`, which is where Prefect keeps it.
    job_variables: dict[str, Any]
    concurrency_limit: int
    tags: list[str]


def deployment_name(miroir_id: str, connection_id: str) -> str:
    return f"{miroir_id}-{connection_id}"


def media_deployment_name(miroir_id: str) -> str:
    """The miroir's media deployment. Identifiers cannot hold `-`, so no
    connection's `<miroir>-<connection>` can be `<miroir>--media`."""
    return f"{miroir_id}--media"


def _entrypoint() -> str:
    """Where a deployment applied now points: what `to_deployment` sets, the
    flow's file relative to the working directory and its function. A deployment
    still pointing at a former path is an update, so apply moves it."""
    from prefect.deployments.runner import RunnerDeployment

    from src.flows.miroir import sync_connection

    deployment = sync_connection.to_deployment(name="entrypoint")
    assert isinstance(deployment, RunnerDeployment)  # see the module docstring
    assert deployment.entrypoint is not None
    return deployment.entrypoint


def _owned(attributes: Any) -> dict[str, Any]:
    """The owned attributes as JSON, in Prefect's serialization."""
    return {
        **{key: attributes[key] for key in OWNED},
        "schedules": [
            schedule.model_dump(mode="json") for schedule in attributes["schedules"]
        ],
    }


@dataclass
class PrefectOrchestrator:
    store: MetadataStore
    # Path of the miroir configuration where flow runs execute (worker or Job).
    config_path: str

    # ---- Deployments ------------------------------------------------------------

    def desired(self, miroir: ResolvedMiroir) -> dict[str, DeploymentSpec]:
        from prefect.client.schemas.schedules import CronSchedule

        declared = miroir.deployment
        if declared is None:
            raise OperationError(
                "No work pool configured: set deployment.work_pool in the configuration"
            )
        job_variables = dict(declared.job_variables or {})
        # Merged over the others for the media deployment alone: in Prefect's
        # database, so a reference the work pool's template resolves, never a key.
        # Top-level keys replace; `image` is never the media deployment's own,
        # so derive runs always run the connections' image.
        media_job_variables = {
            **job_variables,
            **{
                key: value
                for key, value in (declared.media_job_variables or {}).items()
                if key != "image"
            },
        }
        if declared.image:
            job_variables["image"] = declared.image
        if "image" in job_variables:
            media_job_variables["image"] = job_variables["image"]
        tags = ["miroir", self._tag(miroir)]
        specs: dict[str, DeploymentSpec] = {}
        # Only while some connection has a kind the worker makes thumbnails of:
        # without one, no derive run is ever requested.
        if any(
            kind.profile
            for connection in miroir.connections
            for kind in connection.integration.media
        ):
            media = media_deployment_name(miroir.id)
            specs[media] = {
                "name": media,
                # Every run states its connection (submit, schedule_build).
                "parameters": {"config_path": self.config_path},
                "schedules": [],
                "work_pool_name": declared.work_pool,
                "job_variables": media_job_variables,
                "concurrency_limit": CONCURRENCY_LIMIT,
                "tags": [*tags, MEDIA_TAG],
            }
        for connection in miroir.connections:
            schedule = connection.config.schedule
            name = deployment_name(miroir.id, connection.config.id)
            specs[name] = {
                "name": name,
                "parameters": {
                    "connection_id": connection.config.id,
                    "config_path": self.config_path,
                },
                "schedules": [
                    CronSchedule(cron=schedule.cron, timezone=schedule.timezone)
                ]
                if schedule
                else [],
                "work_pool_name": declared.work_pool,
                "job_variables": dict(job_variables),
                "concurrency_limit": CONCURRENCY_LIMIT,
                "tags": list(tags),
            }
        return specs

    def _tag(self, miroir: ResolvedMiroir) -> str:
        return f"miroir:{miroir.id}"

    def actual(self, miroir: ResolvedMiroir) -> dict[str, dict[str, Any]]:
        """What Prefect has for this miroir: per deployment, its ID and the
        owned attributes."""
        from prefect import get_client
        from prefect.client.schemas.filters import (
            DeploymentFilter,
            DeploymentFilterTags,
            FlowFilter,
            FlowFilterName,
        )

        with get_client(sync_client=True) as client:
            deployments = client.read_deployments(
                flow_filter=FlowFilter(name=FlowFilterName(any_=[FLOW_NAME])),
                deployment_filter=DeploymentFilter(
                    tags=DeploymentFilterTags(all_=[self._tag(miroir)])
                ),
            )
        result = {}
        for deployment in deployments:
            limit = deployment.global_concurrency_limit
            result[deployment.name] = {
                "id": str(deployment.id),
                "attributes": {
                    "entrypoint": deployment.entrypoint,
                    "parameters": dict(deployment.parameters or {}),
                    "schedules": [s.schedule for s in deployment.schedules],
                    "work_pool_name": deployment.work_pool_name,
                    "job_variables": dict(deployment.job_variables or {}),
                    "concurrency_limit": limit.limit if limit else None,
                },
            }
        return result

    @_readable_errors
    def plan(self, miroir: ResolvedMiroir) -> list[dict[str, Any]]:
        """Changes needed for Prefect to match the miroir. Read-only."""
        desired, actual = self.desired(miroir), self.actual(miroir)
        entrypoint = _entrypoint()
        changes: list[dict[str, Any]] = []
        for name, spec in sorted(desired.items()):
            want = _owned({**spec, "entrypoint": entrypoint})
            # None for the media deployment, which serves every connection.
            connection_id = spec["parameters"].get("connection_id")
            if name not in actual:
                changes.append(
                    {
                        "action": "create",
                        "deployment": name,
                        "connection_id": connection_id,
                        "set": want,
                    }
                )
                continue
            have = _owned(actual[name]["attributes"])
            diff = {
                key: {"have": have[key], "want": want[key]}
                for key in want
                if have[key] != want[key]
            }
            if diff:
                changes.append(
                    {
                        "action": "update",
                        "deployment": name,
                        "connection_id": connection_id,
                        "changes": diff,
                    }
                )
        for name in sorted(set(actual) - set(desired)):
            changes.append(
                {"action": "delete", "deployment": name, "id": actual[name]["id"]}
            )
        return changes

    @_readable_errors
    def apply(self, miroir: ResolvedMiroir, changes: list[dict[str, Any]]) -> None:
        """Perform a plan. Create and update are the same upsert in Prefect."""
        from prefect import get_client
        from prefect.deployments.runner import RunnerDeployment

        from src.flows.miroir import sync_connection

        desired = self.desired(miroir)
        for change in changes:
            if change["action"] == "delete":
                with get_client(sync_client=True) as client:
                    client.delete_deployment(uuid.UUID(change["id"]))
                continue
            spec = desired[change["deployment"]]
            if change["action"] == "update":
                self._drop_slugged_schedules(spec["name"])
            deployment = sync_connection.to_deployment(**spec)
            assert isinstance(deployment, RunnerDeployment)  # see the module docstring
            deployment.apply()

    def _drop_slugged_schedules(self, name: str) -> None:
        """Prefect updates slugged schedules in place instead of replacing them
        (and refuses an unslugged replacement), so hand-added ones would stay."""
        from prefect import get_client

        with get_client(sync_client=True) as client:
            deployment = client.read_deployment_by_name(f"{FLOW_NAME}/{name}")
            for schedule in deployment.schedules:
                if schedule.slug is not None:
                    client.delete_deployment_schedule(deployment.id, schedule.id)

    # ---- Execution --------------------------------------------------------------

    @_readable_errors
    def submit(self, operation_id: str) -> None:
        """Start a deployment for a requested operation: the media deployment
        for a derive run, the connection's own for anything else.

        The operation ID is the idempotency key: resubmitting after a timeout or
        crash returns the existing flow run instead of starting another. An
        operation that states its earliest start (`not_before`, a debounced
        derive run) is scheduled for then.
        """
        from prefect.client.schemas.objects import FlowRun
        from prefect.deployments import run_deployment
        from prefect.exceptions import ObjectNotFound

        operation = self.store.get_operation(operation_id)
        name = "/".join(
            (
                FLOW_NAME,
                media_deployment_name(operation.miroir_id)
                if operation.kind == "derive"
                else deployment_name(operation.miroir_id, operation.connection_id),
            )
        )
        not_before = json.loads(operation.options or "{}").get("not_before")
        try:
            flow_run = run_deployment(
                name,
                parameters={
                    "connection_id": operation.connection_id,
                    "operation_id": operation.id,
                    "config_path": self.config_path,
                },
                scheduled_time=datetime.fromisoformat(not_before)
                if not_before
                else None,
                timeout=0,
                idempotency_key=operation.id,
            )
        except ObjectNotFound as error:
            raise OperationError(
                f"Deployment {name} does not exist. Apply the deployments first"
                " (Apply deployments in the interface, or `miroir:deploy`)."
            ) from error
        assert isinstance(flow_run, FlowRun)  # see the module docstring
        self.store.bind_backend_run(operation.id, str(flow_run.id))

    @_readable_errors
    def waiting(self, backend_run_id: str) -> bool:
        """Whether the flow run is still scheduled, pending or running."""
        from prefect import get_client
        from prefect.client.schemas.objects import StateType
        from prefect.exceptions import ObjectNotFound

        with get_client(sync_client=True) as client:
            try:
                run = client.read_flow_run(uuid.UUID(backend_run_id))
            except ObjectNotFound:
                return False
        return run.state is not None and run.state.type in (
            StateType.SCHEDULED,
            StateType.PENDING,
            StateType.RUNNING,
        )

    @_readable_errors
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
                outcome: Literal["cancelled", "lost"]
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
                recorded = self.store.close_unverified(
                    item["operation_id"],
                    item["attempt_id"],
                    outcome,
                    f"Prefect flow run ended as {label} without recording an outcome",
                )
                if recorded:
                    closed.append({**item, "outcome": outcome, "state": label})
        return closed
