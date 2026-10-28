"""Resolve one connection's credential references at execution time.

Values are returned to the caller only; nothing is cached, logged, or written to
the process environment. Resolution failures never include the value.
"""

import os
import subprocess
from collections.abc import Callable, Mapping

from src.core.config import ResolvedConnection, SecretReference


class SecretResolutionError(RuntimeError):
    pass


def _env(key: str) -> str | None:
    return os.environ.get(key)


def _fnox(key: str) -> str | None:
    # fnox reads fnox.toml from the working directory and resolves the active profile.
    try:
        result = subprocess.run(
            ["fnox", "get", key],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\n")


PROVIDERS: Mapping[str, Callable[[str], str | None]] = {"env": _env, "fnox": _fnox}


def resolve_credentials(
    connection: ResolvedConnection,
    providers: Mapping[str, Callable[[str], str | None]] = PROVIDERS,
) -> dict[str, str]:
    resolved = {}
    for name in connection.integration.credential_names:
        reference: SecretReference = connection.config.credentials[name]
        value = providers[reference.provider](reference.key)
        if not value:
            raise SecretResolutionError(
                f"Credential {name!r} of connection {connection.config.id!r} "
                f"did not resolve through {reference.provider!r}"
            )
        resolved[name] = value
    return resolved
