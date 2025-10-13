"""Arc Timeline pipeline implementation."""

import dlt
from src.sources import arc_timeline_source
from prefect import flow


@flow
def load_arc_timeline() -> None:
    """Load Arc Timeline data from iCloud Drive."""
    pipeline = dlt.pipeline(
        pipeline_name="arc_timeline",
        destination="duckdb",
        dataset_name="arc_timeline_data",
    )

    load_info = pipeline.run(arc_timeline_source())
    print(load_info)


if __name__ == "__main__":
    load_arc_timeline()
