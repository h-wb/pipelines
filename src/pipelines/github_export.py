"""One-shot import of a GitHub account-data export.

Usage:
    fnox exec -- python -m src.pipelines.github_export /path/to/extracted/export

The warehouse is only reachable in-cluster, and `kubectl port-forward` drops
under bulk COPY, so the 2026-09 import ran on the prefect-worker pod: copy the
export (minus attachments/) and src/ into it, then run the same command there
with the DESTINATION__POSTGRES__* and GITHUB_EXPORT__AUTHOR_EMAILS env set.

Every resource is `replace`, so re-running with a newer export is safe.
"""

import sys

import dlt
from src.sources.github_export import github_export_source

# forks where only my own commits are kept (see github_data.repos.fork)
FORK_REPOS = [
    "h-wb/cert-manager-webhook-ovh",
    "h-wb/codex",
    "h-wb/multi-scrobbler",
    "h-wb/s4-warehouse",
    "h-wb/s7-reseau",
    "h-wb/s9-portail-web",
    "h-wb/s9-simple-chat",
]


def load_github_export(export_dir: str) -> None:
    pipeline = dlt.pipeline(
        pipeline_name="github_export",
        destination="postgres",
        dataset_name="github_export",
    )
    load_info = pipeline.run(
        github_export_source(export_dir=export_dir, fork_repos=FORK_REPOS)
    )
    print(load_info)


if __name__ == "__main__":
    load_github_export(sys.argv[1])
