# Workspace development

This is the implementation record for the personal data workspace. The current
slice is a read-only CLI and validated reference integrations. Production flows
continue to use their existing entry points and deployment configuration.

- [Roadmap](../product-plan.md)
- [Baseline and migration constraints](baseline.md)
- [Design decisions](decisions.md)
- [Implementation log and next steps](implementation-log.md)
- [Synthetic example configuration](../../examples/workspace.json)

## Try the first slice

From the repo root:

```bash
uv run --locked pipelines-workspace integrations
uv run --locked pipelines-workspace --config examples/workspace.json validate
uv run --locked pipelines-workspace --config examples/workspace.json connections
```

Output is JSON. Exit status is 0 for success and 2 for an invalid/unreadable
configuration. The default path is `workspace.json` in the current directory;
that root-level local file is gitignored. Pass `--config` before the subcommand.
`integrations`, `validate`, and `connections` never fetch credentials, connect to
services, start a pipeline, or write configuration. The Python module entry point
is `python -m src.cli`.

`integrations` lists connector metadata and settings schemas. Apple Health currently
advertises the existing `directory_import`, not explicit artifact replay or native
Apple Health XML support.

### Running a connection and reading its status

```bash
uv run --locked pipelines-workspace --config workspace.json run music --duckdb /tmp/mirror.duckdb
uv run --locked pipelines-workspace --config workspace.json status
```

`run` resolves the connection's credentials (`env` or `fnox`), loads into the given
**local DuckDB file only**, and records an operation/attempt. It never writes to the
production warehouse, and it skips dbt (models target Postgres). dlt working state
goes next to the DuckDB file (`<name>.dlt/`). Exit status 1 means the run failed;
the redacted cause is on the attempt record.

`status` shows, per connection: the latest attempt (origin, ingestion and
transformation outcome, load IDs, redacted error), the last successful ingestion
and transformation, whether analysis is `current`, `stale`, or `no_data`, and how
many requested operations have not started. A `running` attempt is marked
`verified: false`: nothing reconciles it with Prefect yet, so a killed worker
leaves it running here.

Both commands use the metadata store at `--metadata` (a SQLAlchemy URL), defaulting
to `$WORKSPACE_METADATA_URL` or `sqlite:///.workspace/metadata.sqlite` (gitignored).

### Importing an export file

```bash
uv run --locked pipelines-workspace submit health ~/Downloads/export.json --role historical
uv run --locked pipelines-workspace import health <artifact-id> --duckdb /tmp/mirror.duckdb
uv run --locked pipelines-workspace artifacts health
```

`submit` validates the file's format, copies it into artifact storage
(`--artifacts`, default `$WORKSPACE_ARTIFACT_DIR` or `.workspace/artifacts`, named
by SHA-256 and read-only), and registers it. It does not load anything. Invalid
files are rejected before anything is stored. Submitting identical bytes again
returns the existing artifact; submitting them with a different role is refused.

`--role` replaces the legacy `manual/` folder convention for Apple Health:
`historical` contributes rows dated before the connection's `cutoff`, `ongoing`
contributes rows from the cutoff onwards.

`import` parses exactly that artifact, with no modification-time cursor, under the
connection lock, into a local DuckDB file. An artifact already imported with the
same parser version and options prints `already_imported` and loads nothing;
`--replay` loads it again (merge-safe: rows keep their source keys). A failed
import never counts as imported. `artifacts` lists each artifact with every import
attempt, its parser version, and options.

The Prefect flow `src/pipelines/workspace.py:sync_connection` uses the same runner
against Postgres and runs dbt. It is not in `prefect.yaml`; see decision 007.

Configuration version 1 currently contains a workspace ID and connections. Each
connection has an integration, validated settings, credential references, and an
optional explicit pipeline/dataset binding. Credentials are objects with `provider`
(`env` or `fnox`) and a `key`; the corresponding resolver is future work. Inline
credential fields and unknown settings are rejected. The configuration's content
revision is a hash of normalized settings/references/bindings, not a deployed revision.

Generated names distinguish accounts and workspaces. Existing connections can be
mapped explicitly, for example:

```json
"binding": {
  "pipeline_name": "listenbrainz",
  "dataset_name": "listenbrainz_data"
}
```

This only describes a binding. It does not adopt, reset, or modify any live state.
The validator rejects duplicate connection IDs, pipeline names, and dataset names
within a workspace. The first design assumes one warehouse per workspace.

Schedules, source selection, destinations, machine-local overlays, and stored
immutable revisions are deliberately absent from this initial schema. Unsupported fields fail validation rather than being ignored.
Add each through a documented schema change when its operation is implemented.

## Validate in an isolated environment

Use a task-specific virtualenv so this does not change a shared worker environment:

```bash
export UV_PROJECT_ENVIRONMENT="$(mktemp -d /tmp/workspace-validation.XXXXXX)"
uv sync --locked --python 3.12 --no-dev --group test
RUNTIME__DLTHUB_TELEMETRY=false "$UV_PROJECT_ENVIRONMENT/bin/python" -m unittest discover -v
"$UV_PROJECT_ENVIRONMENT/bin/pipelines-workspace" --config examples/workspace.json validate
```

The test group adds DuckDB for synthetic source compatibility checks. HTTP calls
from the API source are intercepted; account tokens are fake and destinations are
temporary files. The checks exercise real dlt extraction/loading, independent
account namespaces, repeat imports, and restoration of committed cursor state into
a fresh local working directory. `tests/test_artifacts.py` covers explicit imports: invalid, duplicate, and
overlapping files, changed contents under an unchanged modification time, failed,
interrupted, and crash-window imports, parser version changes, and the metadata
schema upgrade. `tests/test_operations.py` drives the shared runner the
same way: retries, request adoption, same-connection locking, dbt failure, redaction,
metadata outage after a load, and restart persistence. None of this proves
Postgres-specific behavior (including advisory locks), Prefect worker execution,
artifact replay, or crash recovery of pending packages.

For lint and type checks after `uv sync --locked --dev`:

```bash
uv run --locked ruff check src/core src/integrations src/cli src/pipelines/workspace.py tests
uv run --locked ruff format --check src/core src/integrations src/cli src/pipelines/workspace.py tests
uv run --locked mypy --follow-imports=silent src/core src/integrations src/cli src/pipelines/workspace.py
```

## How to continue this PR

For every slice, append a dated entry to the implementation log describing the
problem, changed behavior, validation commands/results, limitations, and next step.
Record architectural choices in the numbered decision log; supersede old decisions
explicitly rather than silently erasing their rationale. Update roadmap status
when its completion criteria are actually met. Keep production cutover a distinct,
reviewable step with its own baseline comparison and recovery procedure.
