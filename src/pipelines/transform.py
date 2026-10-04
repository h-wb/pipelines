"""dbt transformations: staging views and reporting tables for Metabase."""

from pathlib import Path
from typing import Optional

from dbt.cli.main import dbtRunner
from prefect import flow, get_run_logger
from prefect.concurrency.sync import concurrency

DBT_DIR = Path(__file__).parents[2] / "dbt"


def build_models(select: Optional[str] = None) -> None:
    """Build and test the dbt models, limited to `select` when given."""
    args = ["build", "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR)]
    if select:
        args += ["--select", select]
    # one build at a time: loads finishing together would rebuild the same crossover tables
    with concurrency("dbt", occupy=1):
        result = dbtRunner().invoke(args)
    if not result.success:
        raise RuntimeError(f"dbt build failed: {result.exception or 'see the model/test errors above'}")


def refresh_models(source: str) -> None:
    """Rebuild everything downstream of a freshly loaded source.

    A dbt failure is logged, not raised: the load itself succeeded, and the
    nightly `run_dbt` rebuilds everything anyway.
    """
    try:
        build_models(f"source:{source}+")
    except Exception as exc:
        get_run_logger().warning("dbt refresh after the %s load failed: %s", source, exc)


@flow
def run_dbt(select: Optional[str] = None) -> None:
    """Build and test the dbt models.

    Args:
        select: dbt selector to limit the build (e.g. `source:listenbrainz+`);
            every model when omitted.
    """
    build_models(select)


if __name__ == "__main__":
    run_dbt()
