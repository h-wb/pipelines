"""dbt transformations: staging views and reporting tables for Metabase."""

import json
import tempfile
from pathlib import Path

from dbt.cli.main import dbtRunner
from prefect import flow, get_run_logger
from prefect.concurrency.sync import concurrency

from src.core.analysis import AnalysisPlan

DBT_DIR = Path(__file__).parents[2] / "dbt"


def build_models(select: str | None = None, *, variables: dict | None = None) -> None:
    """Build and test the dbt models, limited to `select` when given."""
    args = ["build", "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR)]
    if select:
        args += ["--select", select]
    if variables is not None:
        args += ["--vars", json.dumps(variables)]
    # one build at a time: loads finishing together would rebuild the same crossover tables
    with tempfile.TemporaryDirectory(prefix="miroir-dbt-") as tmp:
        # Fresh parsing and per-run artifacts avoid stale bindings and concurrent
        # processes sharing target/log files. Raw columns may evolve every load.
        args += [
            "--no-partial-parse",
            "--target-path",
            f"{tmp}/target",
            "--log-path",
            f"{tmp}/logs",
        ]
        with concurrency("dbt", occupy=1):
            result = dbtRunner().invoke(args)
    if not result.success:
        raise RuntimeError(
            f"dbt build failed: {result.exception or 'see the model/test errors above'}"
        )


def build_connection_models(plan: AnalysisPlan) -> None:
    build_models(plan.selector, variables=plan.variables)


def refresh_models(source: str) -> None:
    """Rebuild everything downstream of a freshly loaded source.

    A dbt failure is logged, not raised: the load itself succeeded, and the
    nightly `run_dbt` rebuilds everything anyway.
    """
    try:
        build_models(f"source:{source}+")
    except Exception as exc:  # noqa: BLE001 - legacy loads survive model failures
        get_run_logger().warning(
            "dbt refresh after the %s load failed: %s", source, exc
        )


@flow
def run_dbt(select: str | None = None) -> None:
    """Build and test the dbt models.

    Args:
        select: dbt selector to limit the build (e.g. `source:listenbrainz+`);
            every model when omitted.
    """
    build_models(select)


if __name__ == "__main__":
    run_dbt()
