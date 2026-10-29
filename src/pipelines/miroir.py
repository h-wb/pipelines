"""Prefect entry point for miroir connections.

Deployed per connection (decision 013). A flow run needs the miroir
configuration at `config_path`, the metadata database (MIROIR_METADATA_URL or
MIROIR_METADATA__* — see `metadata_url`), and, for built-in secrets,
MIROIR_SECRET_KEY (decision 011).
"""

import os
from pathlib import Path

from prefect import flow
from prefect.runtime import flow_run

from src.core.config import load_miroir, metadata_url
from src.core.operations import MetadataStore
from src.core.runner import run_connection
from src.core.secrets import SecretBox, SecretStore, make_resolver
from src.pipelines.transform import build_connection_models


@flow
def sync_connection(
    connection_id: str,
    operation_id: str | None = None,
    config_path: str = "miroir.json",
) -> dict:
    """Sync one miroir connection into Postgres and rebuild its models.

    Args:
        connection_id: Connection ID from the miroir configuration.
        operation_id: Operation requested through the miroir CLI, if any.
            Runs without one (schedules, n8n) register their own operation.
        config_path: Miroir configuration file on the worker.
    """
    url = metadata_url()
    if url is None:
        raise RuntimeError(
            "Set MIROIR_METADATA_URL or MIROIR_METADATA__HOST/USERNAME/PASSWORD"
        )
    store = MetadataStore(url)
    store.migrate()
    outcome = run_connection(
        load_miroir(Path(config_path)),
        connection_id,
        store=store,
        destination="postgres",
        # Built-in secrets need MIROIR_SECRET_KEY on the worker (decision 011).
        resolve_credentials=make_resolver(
            SecretStore(store, SecretBox.from_environment())
            if os.environ.get("MIROIR_SECRET_KEY")
            else None
        ),
        origin="request" if operation_id else "prefect",
        operation_id=operation_id,
        backend_run_id=str(flow_run.id),
        transform=build_connection_models,
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
