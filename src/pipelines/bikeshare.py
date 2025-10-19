"""Bike Share Toronto pipeline."""

import dlt
from src.sources.bikeshare import bikeshare
from prefect import flow

@flow
def load_bikeshare() -> None:
    """Load Bike Share Toronto trip history."""
    pipeline = dlt.pipeline(
        pipeline_name="bikeshare",
        destination="duckdb",
        dataset_name="bikeshare_data",
    )

    load_info = pipeline.run(bikeshare())
    print(load_info)


if __name__ == "__main__":
    load_bikeshare()
