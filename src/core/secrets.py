"""Connection secrets: encrypted storage and per-connection resolution.

Built-in secrets are encrypted with Fernet (`cryptography`) under a key from
`WORKSPACE_SECRET_KEY` and stored in the metadata database (decision 011).
External references (`env`, `fnox`) resolve through their own tools. Values are
returned to the caller only: never cached, logged, or put in the environment.
Errors never include a value.
"""

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from src.core.config import ResolvedConnection, SecretReference
from src.core.operations import MetadataStore

KEY_VARIABLE = "WORKSPACE_SECRET_KEY"


class SecretResolutionError(RuntimeError):
    pass


class SecretKeyError(RuntimeError):
    """The master key is missing, malformed, or does not decrypt a secret."""


def generate_key() -> str:
    return Fernet.generate_key().decode()


@dataclass(frozen=True)
class SecretBox:
    """Encrypts with the first key and decrypts with any of them (rotation)."""

    fernet: MultiFernet

    @classmethod
    def from_keys(cls, keys: str) -> "SecretBox":
        parts = [part.strip() for part in keys.split(",") if part.strip()]
        if not parts:
            raise SecretKeyError(f"{KEY_VARIABLE} is not set")
        try:
            return cls(MultiFernet([Fernet(part.encode()) for part in parts]))
        except ValueError:
            raise SecretKeyError(
                f"{KEY_VARIABLE} must be Fernet keys (pipelines-workspace generate-key)"
            ) from None

    @classmethod
    def from_environment(cls) -> "SecretBox":
        return cls.from_keys(os.environ.get(KEY_VARIABLE, ""))

    def encrypt(self, value: str) -> bytes:
        return self.fernet.encrypt(value.encode())

    def decrypt(self, token: bytes) -> str:
        try:
            return self.fernet.decrypt(token).decode()
        except InvalidToken:
            raise SecretKeyError(
                f"A stored secret cannot be decrypted with {KEY_VARIABLE}"
            ) from None

    def rotate(self, token: bytes) -> bytes:
        try:
            return self.fernet.rotate(token)
        except InvalidToken:
            raise SecretKeyError(
                f"A stored secret cannot be decrypted with {KEY_VARIABLE}"
            ) from None


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


# External backends resolve a key; the built-in one is bound to a store below.
EXTERNAL: Mapping[str, Callable[[str], str | None]] = {"env": _env, "fnox": _fnox}


@dataclass(frozen=True)
class SecretStore:
    """The built-in backend: encrypted values in the metadata database."""

    store: MetadataStore
    box: SecretBox | None

    def _box(self) -> SecretBox:
        if self.box is None:
            raise SecretKeyError(f"{KEY_VARIABLE} is not set")
        return self.box

    def put(self, connection: ResolvedConnection, name: str, value: str) -> None:
        if name not in connection.integration.credential_names:
            raise SecretResolutionError("Unknown credential for this integration")
        if not value or value.strip() != value:
            raise SecretResolutionError(
                "Credential must be nonempty, without surrounding spaces"
            )
        self.store.put_secret(
            connection.workspace_id,
            connection.config.id,
            name,
            self._box().encrypt(value),
        )

    def get(self, workspace_id: str, connection_id: str, name: str) -> str | None:
        token = self.store.get_secret(workspace_id, connection_id, name)
        return None if token is None else self._box().decrypt(token)

    def rotate_all(self) -> int:
        box = self._box()
        rotated = 0
        for identity, token in self.store.all_secrets():
            self.store.put_secret(**identity, ciphertext=box.rotate(token))
            rotated += 1
        return rotated


def make_resolver(
    secrets: SecretStore | None,
    external: Mapping[str, Callable[[str], str | None]] = EXTERNAL,
) -> Callable[[ResolvedConnection], dict[str, str]]:
    """A resolver for the runner: one connection's credentials, at execution time."""

    def resolve(connection: ResolvedConnection) -> dict[str, str]:
        resolved = {}
        for name in connection.integration.credential_names:
            reference: SecretReference = connection.config.credentials[name]
            if reference.provider == "workspace":
                if secrets is None:
                    raise SecretResolutionError(
                        "Workspace secrets are not available here"
                    )
                value = secrets.get(connection.workspace_id, connection.config.id, name)
            else:
                value = external[reference.provider](str(reference.key))
            if not value:
                raise SecretResolutionError(
                    f"Credential {name!r} of connection {connection.config.id!r} "
                    f"is not set ({reference.provider})"
                )
            resolved[name] = value
        return resolved

    return resolve


def resolve_credentials(
    connection: ResolvedConnection,
    providers: Mapping[str, Callable[[str], str | None]] = EXTERNAL,
) -> dict[str, str]:
    """External references only; kept for callers without a metadata store."""
    return make_resolver(None, providers)(connection)
