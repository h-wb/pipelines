"""Prefect entry point for workspace connections.

Deployed per connection (decision 013). A flow run needs the workspace
configuration at `config_path`, the metadata database (WORKSPACE_METADATA_URL or
WORKSPACE_METADATA__* — see `metadata_url`), and, for built-in secrets,
WORKSPACE_SECRET_KEY (decision 011).
"""

import os
from pathlib import Path

from prefect import flow
from prefect.runtime import flow_run

from src.core.config import load_workspace, metadata_url
from src.core.operations import MetadataStore
from src.core.runner import run_connection
from src.core.secrets import SecretBox, SecretStore, make_resolver
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
    url = metadata_url()
    if url is None:
        raise RuntimeError(
            "Set WORKSPACE_METADATA_URL or WORKSPACE_METADATA__HOST/USERNAME/PASSWORD"
        )
    store = MetadataStore(url)
    store.migrate()
    outcome = run_connection(
        load_workspace(Path(config_path)),
        connection_id,
        store=store,
        destination="postgres",
        # Built-in secrets need WORKSPACE_SECRET_KEY on the worker (decision 011).
        resolve_credentials=make_resolver(
            SecretStore(store, SecretBox.from_environment())
            if os.environ.get("WORKSPACE_SECRET_KEY")
            else None
        ),
        origin="request" if operation_id else "prefect",
        operation_id=operation_id,
        backend_run_id=str(flow_run.id),
        transform=build_models,
        # Overlapping runs of one connection queue instead of failing.
        lock_wait=3600,
    )
    return {
        "operation_id": outcome.operation_id,
        "attempt": outcome.attempt,
        "ingestion": outcome.ingestion,
        "transformation": outcome.transformation,
        "recorded": outcome.recorded,
    }
