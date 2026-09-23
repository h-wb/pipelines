"""Apple Health pipeline implementation."""

from pathlib import Path

import dlt
from prefect import flow

from src.sources.apple_health import apple_health

REPORTING_SQL = Path(__file__).parents[1] / "sql" / "apple_health.sql"


@flow
def load_apple_health() -> None:
    """Load new Apple Health Auto Export files and rebuild the reporting tables."""
    pipeline = dlt.pipeline(
        pipeline_name="apple_health",
        destination="postgres",
        dataset_name="apple_health_data",
    )
    load_info = pipeline.run(apple_health())
    print(load_info)

    with pipeline.sql_client() as client, client.begin_transaction():
        client.execute_sql(REPORTING_SQL.read_text())


if __name__ == "__main__":
    load_apple_health()
