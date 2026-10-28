"""Prefect entry point for workspace connections.

Not deployed yet: `prefect.yaml` still runs the legacy per-source flows. Deploying
this needs `WORKSPACE_METADATA_URL`, a workspace configuration on the worker, and
worker-side credential resolution (see docs/workspace/decisions.md).
"""

import os
from pathlib import Path

from prefect import flow
from prefect.runtime import flow_run

from src.core.config import load_workspace
from src.core.operations import MetadataStore
from src.core.runner import run_connection
from src.core.secrets import resolve_credentials
from src.pipelines.transform import build_models


@flow
def sync_connection(
    connection_id: str,
    operation_id: str | None = None,
    config_path: str = "workspace.json",
) -> dict:
    """Sync one workspace connection into Postgres and rebuild its models.

    Args:
        connection_id: Connection ID from the workspace configuration.
        operation_id: Operation requested through the workspace CLI, if any.
            Runs without one (schedules, n8n) register their own operation.
        config_path: Workspace configuration file on the worker.
    """
    store = MetadataStore(os.environ["WORKSPACE_METADATA_URL"])
    store.migrate()
    outcome = run_connection(
        load_workspace(Path(config_path)),
        connection_id,
        store=store,
        destination="postgres",
        resolve_credentials=resolve_credentials,
        origin="request" if operation_id else "prefect",
        operation_id=operation_id,
        backend_run_id=str(flow_run.id),
        transform=build_models,
    )
    return {
        "operation_id": outcome.operation_id,
        "attempt": outcome.attempt,
        "ingestion": outcome.ingestion,
        "transformation": outcome.transformation,
        "recorded": outcome.recorded,
    }
