"""Browser history pipeline implementation."""

import dlt
from src.sources.browser_history import browser_history_source
from prefect import flow


@flow
def load_browser_history(full_refresh: bool = False) -> None:
    """Load Zen browser history from the Nextcloud snapshot.

    Args:
        full_refresh: When True, empty and reload every resource from scratch.
    """
    pipeline = dlt.pipeline(
        pipeline_name="browser_history",
        destination="postgres",
        dataset_name="browser_history_data",
    )

    load_info = pipeline.run(
        browser_history_source(),
        # truncate, don't drop: dbt views depend on the tables
        refresh="drop_data" if full_refresh else None,
    )
    print(load_info)


if __name__ == "__main__":
    load_browser_history()
