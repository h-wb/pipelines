"""Apple Health pipeline implementation."""

import os
import sys
from pathlib import Path

import dlt
from prefect import flow

from src.sources.apple_health import apple_health


@flow
def load_apple_health(file_paths: list[str] | None = None) -> None:
    if not file_paths:
        data_dir = os.environ["APPLE_HEALTH__DATA_DIR"]
        file_paths = [str(p) for p in Path(data_dir).glob("*.json")]

    print(f"Loading {len(file_paths)} file(s): {file_paths}")

    pipeline = dlt.pipeline(
        pipeline_name="apple_health",
        destination="postgres",
        dataset_name="apple_health_data",
    )
    load_info = pipeline.run(apple_health(file_paths))
    print(load_info)


if __name__ == "__main__":
    load_apple_health(sys.argv[1:] or None)
