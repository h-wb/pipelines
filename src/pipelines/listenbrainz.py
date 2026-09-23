"""ListenBrainz pipeline implementation."""

import dlt
from src.sources import listenbrainz_source
from prefect import flow


@flow
def load_listenbrainz(full_refresh: bool = False) -> None:
    """Load ListenBrainz listening history.

    Args:
        full_refresh: When True, drop and reload the resource from scratch
            (reconciles listens deleted in ListenBrainz and backfilled listens
            older than the incremental cursor).
    """
    pipeline = dlt.pipeline(
        pipeline_name="listenbrainz",
        destination="postgres",
        dataset_name="listenbrainz_data",
    )

    load_info = pipeline.run(
        listenbrainz_source(),
        # truncate, don't drop: dbt views depend on the tables
        refresh="drop_data" if full_refresh else None,
    )
    print(load_info)


if __name__ == "__main__":
    load_listenbrainz()
