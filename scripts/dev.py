"""Launch a checkout with local state or explicitly selected infrastructure."""

import argparse
import fcntl
import os
import secrets
import sys
from collections.abc import Callable
from pathlib import Path

from src.core.config import ConfigurationError, load_miroir, metadata_url
from src.core.secrets import SecretBox, SecretKeyError, generate_key


def saved_secret(path: Path, generate: Callable[[], str]) -> str:
    # Lock creation/read together so concurrent launches reuse the same value.
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        path.chmod(0o600)
        handle.seek(0)
        value = handle.read().strip()
        if not value:
            value = generate()
            handle.write(value + "\n")
        return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    local = modes.add_parser("local", help="Use workspace-local state")
    infra = modes.add_parser("infra", help="Use the existing installation")
    for mode in (local, infra):
        mode.add_argument("--host", default="127.0.0.1")
        mode.add_argument("--port", type=int, default=8765)
    infra.add_argument(
        "--config",
        type=Path,
        default=Path("/miroir/data/miroir/miroir.json"),
        help="Infrastructure installation config (infra mode only)",
    )
    infra.add_argument(
        "--artifacts",
        type=Path,
        default=Path("/miroir/data/miroir/artifacts"),
        help="Shared artifact directory, mounted as seen by flow jobs (infra only)",
    )
    args = parser.parse_args(argv)
    env = dict(os.environ)
    state = Path(".miroir") / ("dev" if args.mode == "local" else "dev-infra")
    command = [sys.executable, "-m", "src.cli"]

    try:
        if args.mode == "local":
            # mise's repo defaults and a user's shell can contain real endpoints.
            env = {
                key: value
                for key, value in env.items()
                if not key.startswith(
                    ("MIROIR_", "PREFECT_", "DESTINATION__POSTGRES__")
                )
            }
            config = state / "miroir.json"
            if config.exists() and load_miroir(config).deployment is not None:
                raise ValueError("Local dev configuration cannot have a deployment")
            command += [
                "--config",
                str(config),
                "serve",
                "--metadata",
                f"sqlite:///{state}/metadata.sqlite",
                "--duckdb",
                str(state / "warehouse.duckdb"),
                "--artifacts",
                str(state / "artifacts"),
            ]
        else:
            missing = [
                key
                for key in (
                    "MIROIR_SECRET_KEY",
                    "PREFECT_API_URL",
                    "PREFECT_API_AUTH_STRING",
                )
                if not env.get(key)
            ]
            if missing:
                raise ValueError(
                    "Missing infrastructure credentials: " + ", ".join(missing)
                )
            url = metadata_url()
            if not url or not url.startswith(
                ("postgresql://", "postgresql+psycopg2://")
            ):
                raise ValueError(
                    "Infrastructure mode requires PostgreSQL metadata settings"
                )
            if not args.config.is_file():
                raise ValueError(
                    "Installation config is missing; mount it or pass --config"
                )
            if load_miroir(args.config).deployment is None:
                raise ValueError(
                    "Infrastructure config must define deployment.work_pool"
                )
            if not args.artifacts.is_dir():
                raise ValueError(
                    "Shared artifacts are missing; mount them or pass --artifacts"
                )
            SecretBox.from_keys(env["MIROIR_SECRET_KEY"])
            env["MIROIR_METADATA_URL"] = url
            env["MIROIR_PREFECT_CONFIG_PATH"] = env.get(
                "MIROIR_PREFECT_CONFIG_PATH", "/miroir/data/miroir/miroir.json"
            )
            command += [
                "--config",
                str(args.config),
                "serve",
                "--artifacts",
                str(args.artifacts),
            ]

        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        state.chmod(0o700)
        env["MIROIR_API_TOKEN"] = saved_secret(
            state / "api-token", lambda: secrets.token_hex(24)
        )
        if args.mode == "local":
            env["MIROIR_SECRET_KEY"] = saved_secret(state / "secret-key", generate_key)
        command += ["--host", args.host, "--port", str(args.port)]
    except (ValueError, OSError, ConfigurationError, SecretKeyError) as error:
        print(f"Cannot start development server: {error}", file=sys.stderr)
        return 2

    print(f"Miroir {args.mode}: login token is in {state / 'api-token'}", flush=True)
    os.execve(sys.executable, command, env)
    return 0  # os.execve replaces this process.


if __name__ == "__main__":
    raise SystemExit(main())
