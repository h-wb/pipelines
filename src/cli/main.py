"""Miroir CLI.

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

from src.core.config import ConfigurationError, load_miroir
from src.integrations.registry import INTEGRATIONS

DEFAULT_METADATA = "sqlite:///.miroir/metadata.sqlite"
DEFAULT_ARTIFACTS = ".miroir/artifacts"
# Requested operations queue behind a running one for the same connection.
EXECUTE_LOCK_WAIT = 3600.0


def main(argv: list[str] | None = None) -> int:
    from src.core.config import metadata_url

    parser = argparse.ArgumentParser(prog="pipelines-miroir")
    parser.add_argument("--config", type=Path, default=Path("miroir.json"))
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
    submit.add_argument(
        "--option",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="An import option of the file's format (e.g. role=historical)",
    )
    submit.add_argument("--format", help="File format ID; the integration's first")
    submit.add_argument(
        "--artifacts",
        type=Path,
        default=Path(os.environ.get("MIROIR_ARTIFACT_DIR", DEFAULT_ARTIFACTS)),
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
    execute = commands.add_parser(
        "execute", help="Run one requested operation (used by the server's executor)"
    )
    execute.add_argument("--duckdb", type=Path, required=True)
    execute.add_argument(
        "--artifacts",
        type=Path,
        default=Path(os.environ.get("MIROIR_ARTIFACT_DIR", DEFAULT_ARTIFACTS)),
        help="Directory where watched folders register their files",
    )
    execute.add_argument("operation")
    serve = commands.add_parser(
        "serve", help="Web interface and HTTP API (needs MIROIR_API_TOKEN)"
    )
    # Only needed without an orchestrator, for the local-subprocess executor;
    # requests run as Prefect flow runs when MIROIR_PREFECT_POOL is set.
    serve.add_argument("--duckdb", type=Path)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--artifacts",
        type=Path,
        default=Path(os.environ.get("MIROIR_ARTIFACT_DIR", DEFAULT_ARTIFACTS)),
        help="Directory for retained original files",
    )
    commands.add_parser(
        "generate-key", help="Print a new MIROIR_SECRET_KEY (Fernet key)"
    )
    credential = commands.add_parser(
        "set-credential",
        help="Store a connection credential in the miroir (reads stdin or prompts)",
    )
    credential.add_argument("connection")
    credential.add_argument("name")
    check = commands.add_parser(
        "check", help="Test a connection's settings and credentials"
    )
    check.add_argument("connection")
    rotate = commands.add_parser(
        "rotate-secrets",
        help="Re-encrypt stored secrets with the first key of MIROIR_SECRET_KEY",
    )
    deployments = commands.add_parser(
        "deployments",
        help="Reconcile Prefect deployments with the miroir (needs MIROIR_PREFECT_POOL)",
    )
    deployments.add_argument("action", choices=("plan", "apply"))
    refresh = commands.add_parser(
        "refresh", help="Close runs Prefect reports as crashed or cancelled"
    )
    models = commands.add_parser(
        "models", help="Rebuild a connection's analysis in Prefect"
    )
    models.add_argument("connection")
    commands_with_store = (status, run, submit, load, listing, execute, serve)
    for command in (
        *commands_with_store,
        credential,
        check,
        rotate,
        deployments,
        refresh,
        models,
    ):
        command.add_argument(
            "--metadata",
            default=metadata_url() or DEFAULT_METADATA,
            help="SQLAlchemy URL of the metadata store",
        )
    args = parser.parse_args(argv)

    result: Any
    if args.command == "generate-key":
        from src.core.secrets import generate_key

        print(generate_key())
        return 0
    if args.command == "serve" and not args.config.exists():
        # A new installation starts with an empty miroir; connections are added
        # through the interface. The parent may not exist yet (e.g. a fresh
        # NFS export mounted at a fixed path).
        args.config.parent.mkdir(parents=True, exist_ok=True)
        args.config.write_text(
            json.dumps(
                {"schema_version": 1, "id": "personal", "connections": []}, indent=2
            )
            + "\n"
        )
        print(
            f"Created an empty miroir configuration at {args.config}",
            file=sys.stderr,
        )
    if args.command == "integrations":
        result = [definition.describe() for definition in INTEGRATIONS.values()]
    else:
        try:
            miroir = load_miroir(args.config)
        except ConfigurationError as error:
            print(f"Configuration error: {error}", file=sys.stderr)
            return 2
        if args.command == "validate":
            result = {
                "valid": True,
                "miroir": miroir.id,
                "revision": miroir.revision,
                "connection_count": len(miroir.connections),
            }
        elif args.command == "connections":
            result = [connection.summary() for connection in miroir.connections]
        else:
            return _operate(args, miroir)
    print(json.dumps(result, indent=2))
    return 0


def _operate(args: argparse.Namespace, miroir: Any) -> int:
    from src.core.operations import MetadataStore
    from src.core.secrets import SecretKeyError

    ids = [c.config.id for c in miroir.connections]
    if getattr(args, "connection", None) not in (None, *ids):
        print("Unknown connection", file=sys.stderr)
        return 2
    if args.metadata == DEFAULT_METADATA:
        Path(".miroir").mkdir(exist_ok=True)
    store = MetadataStore(args.metadata)
    try:
        secrets = _secret_store(store)
        if args.command == "serve":
            return _serve(args, miroir, store, secrets)
        if args.command in ("set-credential", "check", "rotate-secrets"):
            return _credentials(args, store, secrets)
        if args.command in ("deployments", "refresh", "models"):
            return _orchestrate(args, miroir, store, secrets)
        return _with_store(args, miroir, store, ids, secrets)
    except SecretKeyError as error:
        print(str(error), file=sys.stderr)
        return 2
    finally:
        store.engine.dispose()


def _secret_store(store: Any) -> Any:
    """The built-in secret backend, or None when no master key is configured."""
    from src.core.secrets import KEY_VARIABLE, SecretBox, SecretStore

    if not os.environ.get(KEY_VARIABLE):
        return None
    return SecretStore(store, SecretBox.from_environment())


def _orchestrator(args: argparse.Namespace, store: Any, miroir: Any) -> Any:
    """The Prefect adapter when this installation executes through Prefect.

    The configuration's deployment section is authoritative for execution
    settings (decision 016); the MIROIR_* environment variables are fallback
    defaults, so the interface and the CLI plan the same deployments. Either
    source enables the adapter.
    """
    pool = os.environ.get("MIROIR_PREFECT_POOL")
    if not pool and not getattr(miroir, "deployment", None):
        return None
    from src.adapters.prefect import PrefectOrchestrator

    return PrefectOrchestrator(
        store,
        pool,
        # Where flow runs find the configuration (worker or Job mount).
        os.environ.get("MIROIR_PREFECT_CONFIG_PATH", str(args.config.resolve())),
        image=os.environ.get("MIROIR_IMAGE") or None,
        job_variables=json.loads(os.environ.get("MIROIR_PREFECT_JOB_VARIABLES", "{}")),
    )


def _orchestrate(
    args: argparse.Namespace, miroir: Any, store: Any, secrets: Any
) -> int:
    from src.core.service import MiroirService

    store.migrate()
    orchestrator = _orchestrator(args, store, miroir)
    if orchestrator is None:
        print(
            "No Prefect deployment configured: set deployment.work_pool in the"
            " configuration, or MIROIR_PREFECT_POOL as a default",
            file=sys.stderr,
        )
        return 2
    service = MiroirService(
        args.config, store, Path(DEFAULT_ARTIFACTS), orchestrator, secrets, orchestrator
    )
    if args.command == "refresh":
        return _print(service.refresh())
    if args.command == "models":
        return _print({"operation_id": service.request_models(args.connection)})
    if args.action == "plan":
        return _print(service.deployments_plan())
    return _print({"applied": service.deployments_apply()})


def _credentials(args: argparse.Namespace, store: Any, secrets: Any) -> int:
    import getpass

    from src.core.operations import OperationError
    from src.core.secrets import SecretResolutionError
    from src.core.service import InlineExecutor, MiroirService

    store.migrate()
    if args.command == "rotate-secrets":
        if secrets is None:
            print("Set MIROIR_SECRET_KEY first", file=sys.stderr)
            return 2
        return _print({"rotated": secrets.rotate_all()})
    service = MiroirService(
        args.config, store, Path(DEFAULT_ARTIFACTS), InlineExecutor(print), secrets
    )
    if args.command == "check":
        result = service.check_connection(args.connection)
        _print(result)
        return 0 if result["ok"] else 1
    # Never take the value as an argument: it would land in shell history.
    value = (
        sys.stdin.read().strip()
        if not sys.stdin.isatty()
        else getpass.getpass(f"{args.name}: ")
    )
    try:
        service.set_credential(args.connection, args.name, value)
    except (OperationError, SecretResolutionError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return _print({"stored": args.name, "connection": args.connection})


def _with_store(
    args: argparse.Namespace, miroir: Any, store: Any, ids: list, secrets: Any
) -> int:
    from src.core.operations import OperationError

    store.migrate()
    selected = [args.connection] if getattr(args, "connection", None) else ids
    if args.command == "status":
        from src.core.service import InlineExecutor, MiroirService

        service = MiroirService(
            args.config, store, Path(DEFAULT_ARTIFACTS), InlineExecutor(lambda _: None)
        )
        result = [service.connection(c)["status"] for c in selected]
        return _print(result)
    if args.command == "artifacts":
        result = [
            {**artifact, "imports": store.artifact_imports(artifact["id"])}
            for c in selected
            for artifact in store.list_artifacts(miroir.id, c)
        ]
        return _print(result)
    if args.command == "submit":
        from src.core.artifacts import submit_artifact
        from src.integrations.validation import InvalidArtifact

        connection = next(
            c for c in miroir.connections if c.config.id == args.connection
        )
        try:
            options = dict(option.split("=", 1) for option in args.option)
            artifact, created = submit_artifact(
                miroir,
                connection,
                args.file,
                options,
                store=store,
                root=args.artifacts,
                origin="local",
                input_format=args.format,
            )
        except (InvalidArtifact, OperationError, OSError, ValueError) as error:
            print(f"Submit failed: {error}", file=sys.stderr)
            return 1
        return _print({"artifact": artifact, "new": created})

    import dlt

    from src.core.runner import (
        AlreadyImported,
        IngestionFailed,
        execute_operation,
        import_artifact,
        run_connection,
    )
    from src.core.secrets import SecretKeyError, SecretResolutionError, make_resolver

    common: dict[str, Any] = {
        "store": store,
        "destination": dlt.destinations.duckdb(credentials=str(args.duckdb)),
        "resolve_credentials": make_resolver(secrets),
        "pipelines_dir": str(args.duckdb.parent / f"{args.duckdb.stem}.dlt"),
        # Watched folders register their files here, like the server does.
        "artifact_root": getattr(args, "artifacts", None)
        or Path(os.environ.get("MIROIR_ARTIFACT_DIR", DEFAULT_ARTIFACTS)),
    }
    try:
        if args.command == "execute":
            outcome = execute_operation(
                miroir, args.operation, lock_wait=EXECUTE_LOCK_WAIT, **common
            )
        elif args.command == "run":
            outcome = run_connection(miroir, args.connection, origin="local", **common)
        else:
            outcome = import_artifact(
                miroir,
                args.connection,
                args.artifact,
                replay=args.replay,
                origin="local",
                **common,
            )
    except AlreadyImported as error:
        return _print({"already_imported": True, "operation_id": error.operation_id})
    except (
        IngestionFailed,
        SecretResolutionError,
        SecretKeyError,
        OperationError,
    ) as error:
        print(f"Run failed: {error}", file=sys.stderr)
        return 1
    return _print(asdict(outcome))


def _print(result: Any) -> int:
    print(json.dumps(result, indent=2))
    return 0


def _serve(args: argparse.Namespace, miroir: Any, store: Any, secrets: Any) -> int:
    import uvicorn

    from src.core.service import MiroirService, SubprocessExecutor
    from src.web.app import create_app

    token = os.environ.get("MIROIR_API_TOKEN", "")
    if len(token) < 16:
        print("Set MIROIR_API_TOKEN (16+ characters) to serve", file=sys.stderr)
        return 2
    store.migrate()
    orchestrator = _orchestrator(args, store, miroir)
    if orchestrator is None and args.duckdb is None:
        print("Set MIROIR_PREFECT_POOL or pass --duckdb", file=sys.stderr)
        return 2
    executor = orchestrator or SubprocessExecutor(
        [
            "--config",
            str(args.config.resolve()),
            "execute",
            "--duckdb",
            str(args.duckdb.resolve()),
            "--artifacts",
            str(args.artifacts.resolve()),
            "--metadata",
            args.metadata,
        ]
    )
    if secrets is None:
        print(
            "MIROIR_SECRET_KEY is not set: credentials cannot be stored in the "
            "miroir (generate one with `pipelines-miroir generate-key`)",
            file=sys.stderr,
        )
    service = MiroirService(
        args.config.resolve(),
        store,
        args.artifacts.resolve(),
        executor,
        secrets,
        orchestrator,
    )
    uvicorn.run(create_app(service, token), host=args.host, port=args.port)
    return 0
