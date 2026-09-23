"""GitHub pipeline implementation."""

import dlt
from src.sources.github import github_source
from prefect import flow


@flow
def load_github(full_refresh: bool = False) -> None:
    """Load GitHub activity (repos, stars, events, contributions, commits, PRs, issues).

    Args:
        full_refresh: When True, drop and reload every resource from scratch.
    """
    pipeline = dlt.pipeline(
        pipeline_name="github",
        destination="postgres",
        dataset_name="github_data",
    )

    load_info = pipeline.run(
        github_source(),
        refresh="drop_resources" if full_refresh else None,
    )
    print(load_info)


if __name__ == "__main__":
    load_github()
