"""Workspace CLI.

`integrations`, `validate`, and `connections` are read-only: no network, credential
lookup, or runtime imports. `status` reads the metadata store. `run` executes a
connection into a local DuckDB file, never into the production warehouse.
"""

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from src.core.config import ConfigurationError, load_workspace
from src.integrations.registry import INTEGRATIONS

DEFAULT_METADATA = "sqlite:///.workspace/metadata.sqlite"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pipelines-workspace")
    parser.add_argument("--config", type=Path, default=Path("workspace.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("integrations", help="List bundled connector capabilities")
    commands.add_parser("validate", help="Validate desired configuration locally")
    commands.add_parser("connections", help="List validated connection bindings")
    status = commands.add_parser("status", help="Show recorded sync status")
    status.add_argument("connection", nargs="?")
    run = commands.add_parser("run", help="Sync a connection into a local DuckDB file")
    run.add_argument("connection")
    run.add_argument("--duckdb", type=Path, required=True)
    for command in (status, run):
        command.add_argument(
            "--metadata",
            default=os.environ.get("WORKSPACE_METADATA_URL", DEFAULT_METADATA),
            help="SQLAlchemy URL of the metadata store",
        )
    args = parser.parse_args(argv)

    result: Any
    if args.command == "integrations":
        result = [
            {
                "name": definition.name,
                "input_modes": definition.input_modes,
                "input_format": definition.input_format,
                "capabilities": definition.capabilities,
                "credential_names": definition.credential_names,
                "resources": [asdict(resource) for resource in definition.resources],
                "settings_schema": definition.settings_model.model_json_schema(),
            }
            for definition in INTEGRATIONS.values()
        ]
    else:
        try:
            workspace = load_workspace(args.config)
        except ConfigurationError as error:
            print(f"Configuration error: {error}", file=sys.stderr)
            return 2
        if args.command == "validate":
            result = {
                "valid": True,
                "workspace": workspace.id,
                "revision": workspace.revision,
                "connection_count": len(workspace.connections),
            }
        elif args.command == "connections":
            result = [connection.summary() for connection in workspace.connections]
        else:
            return _operate(args, workspace)
    print(json.dumps(result, indent=2))
    return 0


def _operate(args: argparse.Namespace, workspace: Any) -> int:
    from src.core.operations import MetadataStore

    ids = [c.config.id for c in workspace.connections]
    if args.connection is not None and args.connection not in ids:
        print("Unknown connection", file=sys.stderr)
        return 2
    if args.metadata == DEFAULT_METADATA:
        Path(".workspace").mkdir(exist_ok=True)
    store = MetadataStore(args.metadata)
    try:
        return _with_store(args, workspace, store, ids)
    finally:
        store.engine.dispose()


def _with_store(args: argparse.Namespace, workspace: Any, store: Any, ids: list) -> int:
    from src.core.operations import OperationError

    store.migrate()
    if args.command == "status":
        selected = [args.connection] if args.connection else ids
        result = [store.connection_status(workspace.id, c) for c in selected]
        print(json.dumps(result, indent=2))
        return 0

    import dlt

    from src.core.runner import IngestionFailed, run_connection
    from src.core.secrets import SecretResolutionError, resolve_credentials

    try:
        outcome = run_connection(
            workspace,
            args.connection,
            store=store,
            destination=dlt.destinations.duckdb(credentials=str(args.duckdb)),
            resolve_credentials=resolve_credentials,
            origin="local",
            pipelines_dir=str(args.duckdb.parent / f"{args.duckdb.stem}.dlt"),
        )
    except (IngestionFailed, SecretResolutionError, OperationError) as error:
        print(f"Run failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(outcome), indent=2))
    return 0
