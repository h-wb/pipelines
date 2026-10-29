"""Resolve integration analysis metadata into an executable, connection-bound plan."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from src.core.config import ResolvedConnection


@dataclass(frozen=True)
class AnalysisPlan:
    title: str
    selector: str
    variables: dict[str, Any]
    relations: tuple[str, ...]
    version: int = 1

    @property
    def receipt(self) -> str:
        """Compact build identity, retained with a successful attempt (200 chars)."""
        value = json.dumps(
            [self.version, self.selector, self.variables, self.relations],
            sort_keys=True,
        )
        signature = hashlib.sha256(value.encode()).hexdigest()[:24]
        return f"{self.title} v{self.version} ({signature})"

    def summary(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "relations": list(self.relations),
            "timezone": self.variables["timezone"],
            "scope": "Uses this connection only. Cross-source analyses are not enabled yet.",
        }


def analysis_plan(connection: ResolvedConnection) -> AnalysisPlan | None:
    models = connection.integration.models
    if models is None:
        return None
    identity = json.dumps([connection.miroir_id, connection.config.id])
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:16]
    # Every group (including intermediate) fits Postgres's identifier limit.
    namespace = f"miroir_{connection.config.id[:16]}_{suffix}"
    return AnalysisPlan(
        title=models.title,
        selector=models.selector,
        variables={
            "miroir_sources": {models.dbt_source: connection.binding.dataset_name},
            "miroir_namespace": namespace,
            "timezone": connection.config.analysis.timezone,
        },
        relations=tuple(f"{namespace}_{models.group}.{r}" for r in models.relations),
    )
