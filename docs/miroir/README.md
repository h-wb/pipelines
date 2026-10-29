# Miroir development

This is the implementation record for Miroir. The current
slice is a read-only CLI and validated reference integrations. Production flows
continue to use their existing entry points and deployment configuration.

- [Roadmap](../product-plan.md)
- [Baseline and migration constraints](baseline.md)
- [Design decisions](decisions.md)
- [Implementation log and next steps](implementation-log.md)
- [Synthetic example configuration](../../examples/miroir.json)

## Try the first slice

From the repo root:

```bash
uv run --locked pipelines-miroir integrations
uv run --locked pipelines-miroir --config examples/miroir.json validate
uv run --locked pipelines-miroir --config examples/miroir.json connections
```

Output is JSON. Exit status is 0 for success and 2 for an invalid/unreadable
configuration. The default path is `miroir.json` in the current directory;
that root-level local file is gitignored. Pass `--config` before the subcommand.
`integrations`, `validate`, and `connections` never fetch credentials, connect to
services, start a pipeline, or write configuration. The Python module entry point
is `python -m src.cli`.

`integrations` lists connector metadata and settings schemas. Apple Health currently
advertises the existing `directory_import`, not explicit artifact replay or native
Apple Health XML support.

### Running a connection and reading its status

```bash
uv run --locked pipelines-miroir --config miroir.json run music --duckdb /tmp/mirror.duckdb
uv run --locked pipelines-miroir --config miroir.json status
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
`verified: false` until the flow records its outcome or `refresh` (below) closes
it from the orchestrator's state.

Both commands use the metadata store at `--metadata` (a SQLAlchemy URL), defaulting
to `$MIROIR_METADATA_URL` or `sqlite:///.miroir/metadata.sqlite` (gitignored).

### Importing an export file

```bash
uv run --locked pipelines-miroir submit health ~/Downloads/export.json --role historical
uv run --locked pipelines-miroir import health <artifact-id> --duckdb /tmp/mirror.duckdb
uv run --locked pipelines-miroir artifacts health
```

`submit` validates the file's format, copies it into artifact storage
(`--artifacts`, default `$MIROIR_ARTIFACT_DIR` or `.miroir/artifacts`, named
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

### Credentials

Connection secrets (decision 011) are stored encrypted by the miroir under a
master key that you provide:

```bash
export MIROIR_SECRET_KEY="$(uv run --locked pipelines-miroir generate-key)"
# Back this key up separately from the database: stored secrets need it.
```

Add a connection in the web interface (**Add a connection**: settings plus
credentials, then an automatic test), or from the CLI by editing the configuration
with `{"provider": "miroir"}` and storing the value:

```bash
uv run --locked pipelines-miroir set-credential music access_token   # prompts, or reads stdin
uv run --locked pipelines-miroir check music
```

Credentials are write-only: no page, API response, log, or file shows them again.
Connection pages show "set · updated …", Replace, and Test. The status turns
`rejected` when a check or a run is refused (HTTP 401/403) and recovers after a
passing check or a successful load. External references still work, for values
managed elsewhere: `{"provider": "fnox", "key": "LISTENBRAINZ__ACCESS_TOKEN"}` or
`{"provider": "env", "key": "..."}`.

Key rotation: set `MIROIR_SECRET_KEY="<new>,<old>"`, run
`pipelines-miroir rotate-secrets`, then drop the old key.

### Schedules and the Prefect orchestrator

A connection can declare a `schedule` (decision 013), validated with `croniter`
and `zoneinfo`:

```json
"schedule": {"cron": "0 3 * * *", "timezone": "Europe/Paris"}
```

Prefect is the single orchestrator, on a cluster or on one machine (`prefect
server` plus a process worker). The miroir only reconciles with it. Execution
settings live in the configuration (decision 016) — a `deployment` section —
so every writer (the interface, the CLI) plans the same deployments:

```json
"deployment": {
  "work_pool": "miroir-k8s",
  "image": "ghcr.io/h-wb/pipelines:sha-<full-commit>",
  "job_variables": {"namespace": "automation"}
}
```

`image` and `job_variables` are optional; omitted fields fall back to the
`MIROIR_PREFECT_POOL`, `MIROIR_IMAGE`, and `MIROIR_PREFECT_JOB_VARIABLES`
environment variables (bootstrap defaults for a first setup). Either source
enables the Prefect integration. On the cluster, the deployment section should
carry a digest-pinned image — the cluster's registry mirror serves stale
mutable tags.

```bash
uv run --locked pipelines-miroir deployments plan
uv run --locked pipelines-miroir deployments apply   # or: the Apply deployments button
uv run --locked pipelines-miroir refresh
```

Where flow runs find the configuration is an environment concern
(`MIROIR_PREFECT_CONFIG_PATH`, default the local file path) because it differs
between the operator's machine and the Job's mount.

Each connection gets one deployment of `sync_connection`
(`sync-connection/<miroir>-<connection>`) with a concurrency limit of 1.
`plan` reports creates, updates, and removals against what Prefect has — hand
edits in Prefect show as drift; `apply` performs them. With the pool configured,
`serve` submits requested syncs as Prefect flow runs (idempotent on the operation
ID) instead of subprocesses. `refresh` closes attempts whose flow run ended
without recording an outcome: crashed becomes `lost`, cancelled `cancelled`.

On the cluster this is deployed (decisions 012, 015): the image
`ghcr.io/h-wb/pipelines` is built in CI from `uv.lock` (Python 3.12, Prefect
matching the server; `PIPELINES_CODE_REVISION` baked in), a Kubernetes work pool
`miroir-k8s` gives each flow run its own Job with the NFS data export mounted
at `/miroir/data` (configuration and artifacts live under
`/miroir/data/miroir/`), and a dedicated worker runs it. Nothing secret lives
in Prefect, and no database password is copied anywhere: the miroir-k8s base
job template in home-ops injects the warehouse credentials from the
CNPG-generated `postgres-metabase-app` secret, and the metadata credentials from
the `postgres-miroir-app` secret of Miroir's own CNPG cluster — the miroir
composes its metadata URL from the
`MIROIR_METADATA__{HOST,PORT,DATABASE,USERNAME,PASSWORD}` parts
(`MIROIR_METADATA_URL` still wins when set). The only other value a flow run
needs is the miroir master key, in the `miroir-secret` Secret, owned by the
Bitwarden `miroir` item through the ExternalSecret. Reconcile from
a checkout of the repo (its gitignored `miroir.json` must match the NFS copy):

```bash
mise run miroir:deploy     # deployments apply (plan first to review drift)
mise run miroir:refresh    # close unverified runs
```

The tasks get the metadata password from the ProtonPass item
`env-dlt/miroir/metadata-password` (the app-user password of the
postgres-miroir cluster — create the item from the cluster's
`postgres-miroir-app` secret after bootstrap); the other
`MIROIR_METADATA__*` parts are non-secret defaults in `mise.toml`.

Installation secrets (the master key, Postgres roles, Prefect and Metabase
credentials) are environment variables provided by whoever deploys. They are not
stored by the miroir.

### Web interface and HTTP API

```bash
export MIROIR_API_TOKEN="$(openssl rand -hex 24)"
export MIROIR_SECRET_KEY="$(uv run --locked pipelines-miroir generate-key)"
uv run --locked pipelines-miroir --config miroir.json serve
# http://127.0.0.1:8765 — log in with the token. A missing miroir.json is created empty.
```

Without `MIROIR_PREFECT_POOL`, requests run as local subprocesses and need
`--duckdb` (a local DuckDB file); with the pool set, they are submitted as
Prefect flow runs instead.

On the cluster the server is deployed (home-ops `automation/miroir`): the
`miroir` service at `https://miroir.<domain>` behind envoy-internal, on the
same image as the flow runs, with the metadata store in postgres-miroir and
artifacts/configuration on the NFS export. It waits for the `miroir-secret`
Secret — place it by hand until the Bitwarden `miroir` item exists:

```bash
kubectl -n automation create secret generic miroir-secret \
  --from-literal=MIROIR_SECRET_KEY="$(uv run --locked pipelines-miroir generate-key)" \
  --from-literal=MIROIR_API_TOKEN="$(openssl rand -hex 24)"
```

The interface lists connections with their last attempt, when data was last loaded,
analysis freshness (`current`, `stale`, `not_modelled`, `no_data`), and whether
the saved configuration has been applied. Each connection page has sync/scan,
file upload, import/replay, and history. Configuration is edited as JSON with
validation and conflict detection. Pages refresh while work is in flight.
Dashboard and Prefect run links appear when `links` and `dashboards` are configured:

```json
"links": {"prefect_ui": "https://prefect.example", "metabase": "https://metabase.example"},
"connections": [{"id": "health", "...": "...", "dashboards": {"Health": 24}}]
```

Requests run as `pipelines-miroir execute <operation>` subprocesses against
the same local DuckDB file; with `MIROIR_PREFECT_POOL` set they are submitted
as Prefect flow runs instead. Requests for one connection queue behind each other.

The JSON API takes `Authorization: Bearer $MIROIR_API_TOKEN`:

| Method and path | Purpose |
| --- | --- |
| `GET /api/overview`, `GET /api/connections/{id}` | Status, history, files |
| `POST /api/connections/{id}/sync` | Request a sync (202, operation ID) |
| `POST /api/connections/{id}/artifacts?role=ongoing&import=true&name=…` | Store the raw request body as a file, optionally import it |
| `POST /api/connections/{id}/artifacts/{artifact}/import?replay=false` | Request an import |
| `GET /api/config`, `PUT /api/config` with `If-Match: <file_revision>` | Read or replace the configuration |
| `GET /api/integrations` | Integrations with settings schemas and credential fields |
| `POST /api/connections` | Add a connection: `{"integration", "id", "settings", "credentials"}`; returns the check result |
| `PUT /api/connections/{id}/credentials/{name}` | Replace a credential: `{"value": "..."}` (204) |
| `POST /api/connections/{id}/check`, `DELETE /api/connections/{id}` | Test or remove a connection |
| `POST /api/refresh`, `GET /api/deployments/plan` | Reconcile with Prefect (needs `MIROIR_PREFECT_POOL`) |

The artifact endpoint is the replacement for the n8n receiver (decision 009):
point Health Auto Export's REST automation at it with the bearer header.
Uploads are capped at 512 MiB.

The Prefect flow `src/pipelines/miroir.py:sync_connection` uses the same runner
against Postgres and runs dbt. It is not in `prefect.yaml`; see decision 007.

Configuration version 1 currently contains a miroir ID and connections. Each
connection has an integration, validated settings, credential references, and an
optional explicit pipeline/dataset binding. Credentials are objects with `provider`
(`env` or `fnox`) and a `key`; the corresponding resolver is future work. Inline
credential fields and unknown settings are rejected. The configuration's content
revision is a hash of normalized settings/references/bindings, not a deployed revision.

Generated names distinguish accounts and miroirs. Existing connections can be
mapped explicitly, for example:

```json
"binding": {
  "pipeline_name": "listenbrainz",
  "dataset_name": "listenbrainz_data"
}
```

This only describes a binding. It does not adopt, reset, or modify any live state.
The validator rejects duplicate connection IDs, pipeline names, and dataset names
within a miroir. The first design assumes one warehouse per miroir.

Schedules, source selection, destinations, machine-local overlays, and stored
immutable revisions are deliberately absent from this initial schema. Unsupported fields fail validation rather than being ignored.
Add each through a documented schema change when its operation is implemented.

## Validate in an isolated environment

Use a task-specific virtualenv so this does not change a shared worker environment:

```bash
export UV_PROJECT_ENVIRONMENT="$(mktemp -d /tmp/miroir-validation.XXXXXX)"
uv sync --locked --python 3.12 --no-dev --group test
RUNTIME__DLTHUB_TELEMETRY=false "$UV_PROJECT_ENVIRONMENT/bin/python" -m unittest discover -v
"$UV_PROJECT_ENVIRONMENT/bin/pipelines-miroir" --config examples/miroir.json validate
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
uv run --locked ruff check src/core src/integrations src/cli src/pipelines/miroir.py tests
uv run --locked ruff format --check src/core src/integrations src/cli src/pipelines/miroir.py tests
uv run --locked mypy --follow-imports=silent src/core src/integrations src/cli src/pipelines/miroir.py
```

## How to continue this PR

For every slice, append a dated entry to the implementation log describing the
problem, changed behavior, validation commands/results, limitations, and next step.
Record architectural choices in the numbered decision log; supersede old decisions
explicitly rather than silently erasing their rationale. Update roadmap status
when its completion criteria are actually met. Keep production cutover a distinct,
reviewable step with its own baseline comparison and recovery procedure.
