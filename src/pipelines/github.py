"""GitHub pipeline implementation.

Nightly: `load_github()`. One-shot seed from an account-data export:
    fnox exec -- python -m src.pipelines.github /path/to/extracted/export
which loads the raw `export_*` tables and (re)starts the repo-level API
resources from the export date. The warehouse is only reachable in-cluster
(and `kubectl port-forward` drops under bulk COPY), so run the seed on the
prefect-worker pod with the export copied in (attachments/ not needed).
"""

import sys
from typing import Optional

import dlt
from src.sources.github import EXPORT_RESOURCES, REPO_RESOURCES, github_source
from prefect import flow


@flow
def load_github(full_refresh: bool = False, export_dir: Optional[str] = None) -> None:
    """Load GitHub activity.

    Args:
        full_refresh: When True, drop and reload every resource from scratch.
        export_dir: Seed from this extracted export: empties and reloads the
            repo-level and export resources (plus stars, reloaded in full).
    """
    pipeline = dlt.pipeline(
        pipeline_name="github",
        destination="postgres",
        dataset_name="github_data",
    )

    source = github_source(export_dir=export_dir)
    if export_dir:
        source = source.with_resources(*REPO_RESOURCES, *EXPORT_RESOURCES, "stars")
    load_info = pipeline.run(
        source,
        # truncate, don't drop: dbt views depend on the tables
        refresh="drop_data" if full_refresh or export_dir else None,
    )
    print(load_info)


if __name__ == "__main__":
    load_github(export_dir=sys.argv[1] if len(sys.argv) > 1 else None)
