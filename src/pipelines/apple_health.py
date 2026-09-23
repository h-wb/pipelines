"""Apple Health pipeline implementation."""

import dlt
from prefect import flow

from src.sources.apple_health import apple_health


@flow
def load_apple_health() -> None:
    """Load new Apple Health Auto Export files."""
    pipeline = dlt.pipeline(
        pipeline_name="apple_health",
        destination="postgres",
        dataset_name="apple_health_data",
    )
    load_info = pipeline.run(apple_health())
    print(load_info)


if __name__ == "__main__":
    load_apple_health()
