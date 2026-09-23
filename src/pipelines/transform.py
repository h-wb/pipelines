"""dbt transformations: staging views and reporting tables for Metabase."""

from pathlib import Path

from dbt.cli.main import dbtRunner
from prefect import flow

DBT_DIR = Path(__file__).parents[2] / "dbt"


@flow
def run_dbt() -> None:
    """Build and test every dbt model."""
    result = dbtRunner().invoke(["build", "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR)])
    if not result.success:
        raise RuntimeError(f"dbt build failed: {result.exception or 'see the model/test errors above'}")


if __name__ == "__main__":
    run_dbt()
