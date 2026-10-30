"""Resolve a configured analysis (decision 022) into an executable dbt plan."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from src.core.config import ResolvedAnalysis, ResolvedMiroir

# Unlinked inputs use this placeholder and are excluded from table discovery;
# they never fall back to a legacy source schema.
UNLINKED = "miroir_unlinked_input"


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
        }


def analysis_plan(analysis: ResolvedAnalysis) -> AnalysisPlan:
    package = analysis.package
    identity = json.dumps([analysis.miroir_id, analysis.id])
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:16]
    # Every group (including intermediate) fits Postgres's identifier limit.
    namespace = f"miroir_{analysis.id[:16]}_{suffix}"
    variables: dict[str, Any] = {
        "miroir_sources": {
            spec.dbt_source: analysis.inputs[role].binding.dataset_name
            if role in analysis.inputs
            else UNLINKED
            for role, spec in package.inputs.items()
        },
        "miroir_namespace": namespace,
        "timezone": analysis.config.timezone,
        **package.variables(
            {role: c.settings for role, c in analysis.inputs.items()},
            analysis.config.parameters,
        ),
    }
    if any(not spec.required for spec in package.inputs.values()):
        # Models report measures of unlinked inputs as unknown, not zero.
        variables["miroir_inputs"] = sorted(analysis.inputs)
        variables["miroir_unlinked_sources"] = [
            spec.dbt_source
            for role, spec in package.inputs.items()
            if role not in analysis.inputs
        ]
    return AnalysisPlan(
        title=package.title,
        selector=package.selector,
        variables=variables,
        relations=tuple(f"{namespace}_{package.group}.{r}" for r in package.relations),
        version=package.version,
    )


def analyses_linking(
    miroir: ResolvedMiroir, connection_id: str
) -> tuple[ResolvedAnalysis, ...]:
    return tuple(a for a in miroir.analyses if a.links(connection_id))


def find_analysis(miroir: ResolvedMiroir, analysis_id: str) -> ResolvedAnalysis:
    from src.core.operations import OperationError

    for analysis in miroir.analyses:
        if analysis.id == analysis_id:
            return analysis
    raise OperationError("Unknown analysis")
