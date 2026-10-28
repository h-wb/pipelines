"""Read and validate desired configuration without fetching secrets or running jobs."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator

from src.core.models import DatabaseIdentifier, Identifier, SecretKey, StrictModel
from src.integrations.registry import INTEGRATIONS, IntegrationDefinition


class ConfigurationError(ValueError):
    """A safe diagnostic that must not include configuration input values."""


class SecretReference(StrictModel):
    provider: Literal["env", "fnox"]
    key: SecretKey


class Binding(StrictModel):
    pipeline_name: DatabaseIdentifier
    dataset_name: DatabaseIdentifier


class ConnectionConfig(StrictModel):
    id: Identifier
    integration: Identifier
    settings: dict[str, Any]
    credentials: dict[str, SecretReference] = Field(default_factory=dict)
    binding: Binding | None = None


class WorkspaceConfig(StrictModel):
    schema_version: Literal[1]
    id: Identifier
    connections: list[ConnectionConfig] = Field(min_length=1)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("expected an integer schema version")
        return value


@dataclass(frozen=True)
class ResolvedConnection:
    config: ConnectionConfig
    integration: IntegrationDefinition
    settings: StrictModel
    binding: Binding

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.config.id,
            "integration": self.integration.name,
            "binding": self.binding.model_dump(),
            "explicit_binding": self.config.binding is not None,
            "credential_names": list(self.integration.credential_names),
            "resources": [resource.name for resource in self.integration.resources],
            "capabilities": list(self.integration.capabilities),
        }


@dataclass(frozen=True)
class ResolvedWorkspace:
    id: str
    revision: str
    connections: tuple[ResolvedConnection, ...]


def _validation_error(error: ValidationError, prefix: str) -> ConfigurationError:
    # Pydantic messages can include supplied values. Emit only known field paths
    # and machine error codes. Unknown keys are not echoed either.
    issues = []
    for issue in error.errors(include_input=False, include_context=False):
        location = list(issue["loc"])
        if issue["type"] == "extra_forbidden" and location:
            location[-1] = "<unknown field>"
        # Credential keys are user-controlled and may themselves contain a secret.
        if "credentials" in location:
            i = location.index("credentials") + 1
            if i < len(location):
                location[i] = "<credential>"
        issues.append(f"{prefix}.{'.'.join(map(str, location))}: {issue['type']}")
    return ConfigurationError("; ".join(issues))


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError("Duplicate JSON object key")
        result[key] = value
    return result


def parse_workspace(text: str) -> ResolvedWorkspace:
    try:
        raw = json.loads(text, object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError) as error:
        raise ConfigurationError("Invalid JSON configuration") from error
    try:
        config = WorkspaceConfig.model_validate(raw)
    except ValidationError as error:
        raise _validation_error(error, "workspace") from error

    connections = []
    ids: set[str] = set()
    pipelines: set[str] = set()
    datasets: set[str] = set()
    for index, connection in enumerate(config.connections):
        prefix = f"connections.{index}"
        definition = INTEGRATIONS.get(connection.integration)
        if definition is None:
            raise ConfigurationError(f"{prefix}.integration: unknown integration")
        try:
            settings = definition.settings_model.model_validate(connection.settings)
        except ValidationError as error:
            raise _validation_error(error, f"{prefix}.settings") from error
        if set(connection.credentials) != set(definition.credential_names):
            raise ConfigurationError(
                f"{prefix}.credentials: incorrect credential names"
            )
        # Length-prefix the workspace ID so (a_b, c) and (a, b_c) cannot alias.
        name = f"ws_{len(config.id)}_{config.id}_{connection.id}"
        binding = connection.binding or Binding(pipeline_name=name, dataset_name=name)
        for value, seen, label in (
            (connection.id, ids, "id"),
            (binding.pipeline_name, pipelines, "pipeline_name"),
            (binding.dataset_name, datasets, "dataset_name"),
        ):
            if value in seen:
                raise ConfigurationError(
                    f"{prefix}.{label}: duplicate binding or identity"
                )
            seen.add(value)
        connections.append(
            ResolvedConnection(connection, definition, settings, binding)
        )

    # Normalize defaults, typed settings, and bindings before computing the revision.
    # It identifies desired configuration only; nothing is persisted or deployed here.
    normalized = {
        "schema_version": config.schema_version,
        "id": config.id,
        "connections": [
            {
                "id": c.config.id,
                "integration": c.integration.name,
                "settings": c.settings.model_dump(),
                "credentials": {
                    k: v.model_dump() for k, v in c.config.credentials.items()
                },
                "binding": c.binding.model_dump(),
            }
            for c in sorted(connections, key=lambda item: item.config.id)
        ],
    }
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    revision = hashlib.sha256(canonical.encode()).hexdigest()
    return ResolvedWorkspace(config.id, revision, tuple(connections))


def load_workspace(path: Path) -> ResolvedWorkspace:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ConfigurationError("Cannot read UTF-8 configuration file") from error
    return parse_workspace(text)
