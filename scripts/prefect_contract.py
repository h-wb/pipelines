"""The Prefect adapter's tests against a Prefect server of another version.

The suite's own harness starts a server from the locked client's package, so it
can never see a client and server that differ. This starts `prefect server
start` from the version asked for, in an environment of its own, and runs
tests/test_prefect.py against it (the Prefect version contract, docs/decisions).

    uv run --locked python -m scripts.prefect_contract locked
    uv run --locked python -m scripts.prefect_contract latest
    uv run --locked python -m scripts.prefect_contract 3.10.0
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The runtime image's Python.
PYTHON = "3.12"
STARTUP_SECONDS = 180


def command(server: str) -> list[str]:
    """How to run the `prefect` CLI of a server version. The locked one is this
    environment's, so its dependencies are locked too; another comes from an
    environment of its own."""
    if server == "locked":
        return [str(Path(sys.executable).parent / "prefect")]
    return [
        *("uv", "tool", "run", "--isolated", "--python", PYTHON),
        *("--from", requirement(server), "prefect"),
    ]


def requirement(server: str) -> str:
    """What `uv tool run --from` installs for a server version."""
    if server == "latest":
        import prefect

        major = prefect.__version__.split(".")[0]
        return f"prefect>={major},<{int(major) + 1}"
    return f"prefect=={server}"


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def clean_environment(**extra: str) -> dict[str, str]:
    """This shell's environment without its Prefect settings: no
    installation's API URL or credentials reach the server or the tests."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PREFECT_")
    }
    return {**environment, **extra}


def wait_for(api: str, server: subprocess.Popen[bytes]) -> str:
    """The server's version, once it answers."""
    deadline = time.monotonic() + STARTUP_SECONDS
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise SystemExit(f"The Prefect server exited with {server.returncode}")
        try:
            with urllib.request.urlopen(f"{api}/admin/version", timeout=5) as response:
                return str(json.load(response))
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(1)
    raise SystemExit(f"The Prefect server did not answer within {STARTUP_SECONDS}s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "server", help="`locked`, `latest` (of the locked major) or a version"
    )
    args = parser.parse_args()
    port = free_port()
    api = f"http://127.0.0.1:{port}/api"
    with tempfile.TemporaryDirectory() as home:
        server = subprocess.Popen(
            [
                *command(args.server),
                *("server", "start", "--host", "127.0.0.1"),
                *("--port", str(port)),
            ],
            env=clean_environment(
                PREFECT_HOME=home,
                PREFECT_SERVER_ANALYTICS_ENABLED="false",
                PREFECT_UI_ENABLED="false",
            ),
        )
        try:
            version = wait_for(api, server)
            print(f"Prefect server {version} at {api}", flush=True)
            return subprocess.run(
                [sys.executable, "-m", "unittest", "-v", "tests.test_prefect"],
                cwd=ROOT,
                env=clean_environment(
                    MIROIR_TEST_PREFECT_API_URL=api,
                    PREFECT_HOME=str(Path(home) / "client"),
                ),
                check=False,
            ).returncode
        finally:
            server.terminate()
            try:
                server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == "__main__":
    sys.exit(main())
