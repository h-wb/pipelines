"""ListenBrainz pipeline implementation."""

import dlt
from src.sources import listenbrainz_source
from prefect import flow


@flow
def load_listenbrainz() -> None:
    """Load ListenBrainz listening history."""
    pipeline = dlt.pipeline(
        pipeline_name="listenbrainz",
        destination=dlt.destinations.duckdb("/root/.prefect/data.db"),
        dataset_name="listenbrainz_data",
    )

    load_info = pipeline.run(listenbrainz_source())
    print(load_info)


if __name__ == "__main__":
    load_listenbrainz()
