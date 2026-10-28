"""Workspace CLI.

`integrations`, `validate`, and `connections` are read-only: no network, credential
lookup, or runtime imports. `status` and `artifacts` read the metadata store.
`submit` stores and registers a file without loading it. `run` and `import` load
into a local DuckDB file, never into the production warehouse.
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
DEFAULT_ARTIFACTS = ".workspace/artifacts"


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
    submit = commands.add_parser("submit", help="Store and register an export file")
    submit.add_argument("connection")
    submit.add_argument("file", type=Path)
    submit.add_argument("--role", choices=("historical", "ongoing"), required=True)
    submit.add_argument(
        "--artifacts",
        type=Path,
        default=Path(os.environ.get("WORKSPACE_ARTIFACT_DIR", DEFAULT_ARTIFACTS)),
        help="Directory for retained original files",
    )
    load = commands.add_parser(
        "import", help="Load a registered artifact into a local DuckDB file"
    )
    load.add_argument("connection")
    load.add_argument("artifact")
    load.add_argument("--duckdb", type=Path, required=True)
    load.add_argument(
        "--replay", action="store_true", help="Load again even if already imported"
    )
    listing = commands.add_parser("artifacts", help="List artifacts and their imports")
    listing.add_argument("connection", nargs="?")
    for command in (status, run, submit, load, listing):
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
    selected = [args.connection] if args.connection else ids
    if args.command == "status":
        result = [store.connection_status(workspace.id, c) for c in selected]
        return _print(result)
    if args.command == "artifacts":
        result = [
            {**artifact, "imports": store.artifact_imports(artifact["id"])}
            for c in selected
            for artifact in store.list_artifacts(workspace.id, c)
        ]
        return _print(result)
    connection = next(
        c for c in workspace.connections if c.config.id == args.connection
    )
    if args.command == "submit":
        from src.core.artifacts import submit_artifact
        from src.integrations.formats import InvalidArtifact

        try:
            artifact, created = submit_artifact(
                workspace,
                connection,
                args.file,
                args.role,
                store=store,
                root=args.artifacts,
            )
        except (InvalidArtifact, OperationError, OSError) as error:
            print(f"Submit failed: {error}", file=sys.stderr)
            return 1
        return _print({"artifact": artifact, "new": created})

    import dlt

    from src.core.runner import (
        AlreadyImported,
        IngestionFailed,
        import_artifact,
        run_connection,
    )
    from src.core.secrets import SecretResolutionError, resolve_credentials

    common: dict[str, Any] = {
        "store": store,
        "destination": dlt.destinations.duckdb(credentials=str(args.duckdb)),
        "resolve_credentials": resolve_credentials,
        "origin": "local",
        "pipelines_dir": str(args.duckdb.parent / f"{args.duckdb.stem}.dlt"),
    }
    try:
        if args.command == "run":
            outcome = run_connection(workspace, args.connection, **common)
        else:
            outcome = import_artifact(
                workspace, args.connection, args.artifact, replay=args.replay, **common
            )
    except AlreadyImported as error:
        return _print({"already_imported": True, "operation_id": error.operation_id})
    except (IngestionFailed, SecretResolutionError, OperationError) as error:
        print(f"Run failed: {error}", file=sys.stderr)
        return 1
    return _print(asdict(outcome))


def _print(result: Any) -> int:
    print(json.dumps(result, indent=2))
    return 0
