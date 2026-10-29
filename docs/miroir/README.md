# Miroir development

Miroir provides a web interface, HTTP API, and CLI over shared connection,
artifact, and execution services. The server and isolated Prefect Kubernetes
runtime are deployed; ListenBrainz has completed an end-to-end sync. Legacy
flows continue alongside Miroir until each source is explicitly cut over.

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

`integrations` lists connector metadata: settings schemas, credentials, resources,
and **inputs** (decision 021). An input is either a **fetch** (an API pull with the
connection's credentials) or a **file format** (with its own import options).
ListenBrainz, GitHub (personal access token for the configured login; the token's
account is checked), and Bike Share Toronto (mobile-app member ID and authorization
token) fetch. Apple Health reads Health Auto Export JSON (option `role`:
`ongoing`/`historical`); browser history reads Firefox/Zen Places snapshots, plain or
gzip. Native Apple Health XML is not supported. Each connector package under
`src/integrations/<name>/` owns its settings, credentials, check, resources, inputs,
and dlt sources; the registry only lists them. `src/sources/` and the old flow entry
points are the legacy path.

Files arrive through three core transports, all of which register an artifact and
then import it: upload, the connection's receiving URL, and a **watched folder**. A
folder is connection configuration, not a source setting:

```json
"folder": {"path": "/miroir/data/browser", "pattern": "*.sqlite.gz",
           "options": {}, "keep_last": 5}
```

`pattern` defaults to the format's, `options` to its defaults, and `format` to the
connector's first. Each sync scans the folder, registers new content (identical
bytes map to their existing artifact), imports each pending file as its own import
operation linked to the sync, and builds the analysis once. A scan that finds
nothing new loads and builds nothing. Folder files keep the latest `keep_last`
stored copies (`null` keeps all); older imported copies are removed, their receipts
and history kept, and replaying them is refused until the file arrives again. A
pre-021 `settings.data_dir` is read as a folder.

### Running a connection and reading its status

```bash
uv run --locked pipelines-miroir --config miroir.json run music --duckdb /tmp/mirror.duckdb
uv run --locked pipelines-miroir --config miroir.json status
```

`run` resolves the connection's credentials (`miroir`, `env`, or `fnox`), loads into the given
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
uv run --locked pipelines-miroir submit health ~/Downloads/export.json --option role=historical
uv run --locked pipelines-miroir import health <artifact-id> --duckdb /tmp/mirror.duckdb
uv run --locked pipelines-miroir artifacts health
```

`submit` validates the file's format, copies it into artifact storage
(`--artifacts`, default `$MIROIR_ARTIFACT_DIR` or `.miroir/artifacts`, named
by SHA-256 and read-only), and registers it. It does not load anything. Invalid
files are rejected before anything is stored. Submitting identical bytes again
returns the existing artifact; submitting them with different import options is
refused.

`--option NAME=VALUE` sets the format's import options (`--format` picks a format
when a connector has several). For Apple Health, `role=historical` contributes rows
dated before the connection's `cutoff`, `role=ongoing` (the default) rows from the
cutoff onwards.

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
uv run --locked pipelines-miroir --config miroir.json serve --duckdb /tmp/miroir.duckdb
# http://127.0.0.1:8765 — log in with the token. A missing miroir.json is created empty.
```

Without a configured work pool, requests run as local subprocesses and need
`--duckdb` (a local DuckDB file). With `deployment.work_pool` in the configuration
or `MIROIR_PREFECT_POOL` set, they are submitted as Prefect flow runs instead.

Behind a TLS-terminating proxy, set Uvicorn's `FORWARDED_ALLOW_IPS` to the trusted
proxy addresses/networks. The cluster trusts its ingress pod network; this makes
generated receiving URLs use HTTPS and login cookies carry Secure. Do not rely on
the localhost-only default when the proxy runs in another pod.

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

Health and music analysis rebuild automatically after successful Postgres imports
and syncs. Each connection reads its own bound raw dataset and writes separate
`miroir_<connection>_<identity>_{staging,health,music}` schemas; **Model outputs**
on Activity lists the exact reporting relations. Even an explicit legacy raw binding
writes isolated model outputs. Existing `health.*`, `music.*`, and legacy dashboards
are preserved. Point connection-specific Metabase questions at the listed output
relations, then configure their dashboard links as above. Miroir does not yet
create/reconcile dashboard assets on every installation.
[`examples/analysis-dashboards.json`](../../examples/analysis-dashboards.json)
provides source-local SQL/chart seeds with logical `{health}`/`{music}` schema
placeholders. Resolve these against the listed output schema, create new questions
and dashboards in Metabase, and retain their IDs in the connection's `dashboards`.
The examples never reference this installation's backend IDs or overwrite assets.

Health, music, GitHub, biking, and browser builds are independent: only their own
models and ancestors are selected. Analyses live in `src/analyses/catalog.py`;
integrations recommend one by ID. Cross-source models remain on the legacy path until their health,
music, and bike-share inputs can be explicitly linked. Missing optional columns
and resources in small exports produce nulls/empty tables, rather than failing or
substituting zero activity. Existing raw tables missing required identity/time
fields fail clearly. Legacy dbt commands retain their original source schemas,
output schemas, and column types.

**Rebuild analysis** on Activity requests a model-only operation through the same
executor/connection lock. It does not fetch source credentials, load rows, or change
the last-ingestion time. Its result appears separately in history; model failure
does not undo ingestion. Analysis is current only after a successful build after
the latest load using the current configuration and analysis-plan version. The CLI
equivalent is `pipelines-miroir --config miroir.json models <connection>` (Prefect
deployment required). Local DuckDB execution keeps skipping Postgres models.

**Analysis settings** contains an IANA timezone (UTC by default), stored as
`"analysis": {"timezone": "America/Toronto"}` on the connection. It controls music
days/hours. Health dates retain the exported local date. Save and rebuild to apply
changes to already loaded data. Analysis settings are separate from source settings,
delivery settings, credentials, and scheduling.

Requests run as `pipelines-miroir execute <operation>` subprocesses against
the same local DuckDB file; with `MIROIR_PREFECT_POOL` set they are submitted
as Prefect flow runs instead. Requests for one connection queue behind each other.

The JSON API takes `Authorization: Bearer $MIROIR_API_TOKEN`:

| Method and path | Purpose |
| --- | --- |
| `GET /api/overview`, `GET /api/connections/{id}` | Status, history, files |
| `POST /api/connections/{id}/sync` | Request a sync (202, operation ID) |
| `POST /api/connections/{id}/models` | Request analysis rebuild without ingestion (202, operation ID); requires previously loaded data |
| `PUT /api/connections/{id}/analysis` with `If-Match: <file_revision>` | Update analysis settings, e.g. `{"timezone": "America/Toronto"}` |
| `POST /api/connections/{id}/artifacts?import=true&name=…&<option>=…` | Store the raw request body as a file, optionally import it |
| `POST /api/connections/{id}/artifacts/{artifact}/import?replay=false` | Request an import |
| `POST /api/connections/{id}/delivery-token`, `DELETE /api/connections/{id}/delivery-token` | Create/replace (value shown once) or revoke a receive-only token; requires administrator auth |
| `POST /api/connections/{id}/receive` | Receive an ongoing export and request an import; requires that connection's delivery bearer token |
| `GET /api/config`, `PUT /api/config` with `If-Match: <file_revision>` | Read or replace the configuration |
| `GET /api/integrations` | Integrations with settings schemas and credential fields |
| `POST /api/connections` | Add a connection: `{"integration", "id", "settings", "credentials"}`; returns the check result |
| `PUT /api/connections/{id}/settings` with `If-Match: <file_revision>` | Patch non-secret settings; keeps other connection configuration |
| `POST /api/connections/{id}/prepare` | Prepare execution for this connection only |
| `PUT /api/connections/{id}/credentials/{name}` | Replace a credential: `{"value": "..."}` (204) |
| `POST /api/connections/{id}/check`, `DELETE /api/connections/{id}` | Test or remove a connection |
| `POST /api/refresh`, `GET /api/deployments/plan` | Reconcile with Prefect (needs `MIROIR_PREFECT_POOL`) |

### Uploading and automatically receiving Apple Health exports

Add an **Apple Health (Health Auto Export)** connection, then choose how data
arrives: **Upload files** or **Health Auto Export**. **Advanced delivery methods**
offers **Watch a server folder**. Enter a connection ID and cutoff date; only the
folder method asks for a server path. Source credentials are not required.

Continue into first-data setup. Upload setup offers a file picker immediately.
Automatic setup offers **Enable automatic delivery**, which prepares execution
for this connection and generates its receiving token. Follow the sender steps,
then choose **I've configured my sender — check delivery** to wait for the first
automatic export and its import result. Viewing or retrying setup never replaces
an active token. Manual uploads do not count as proof that a sender has connected.
ListenBrainz follows the same journey with account settings, source credentials,
and a first-sync action. Execution actions prepare only their connection, so there
is no separate global Apply deployments step for first use.

On an existing connection, **Activity** contains receipts, uploads, imports, and
history. **Settings** contains account/parser settings, source credentials,
**Data delivery**, and removal. Non-secret edits use the same conflict checks as
the configuration editor. Uploads remain available alongside automatic delivery;
server folder scanning can be added or removed independently. A receiving-only
check describes readiness, never proof of phone connectivity.

On the connection page, **Upload an export** accepts a Health Auto Export JSON
file and defaults to importing it immediately. Historical files contribute dates
before the cutoff; ongoing files contribute dates from the cutoff onwards.

For automatic delivery, copy the URL and Authorization header shown during setup.
For an existing connection, open **Settings → Data delivery → Set up automatic
delivery**. In Health Auto Export, create a
REST API automation with JSON format and these settings:

```http
POST https://miroir.example/api/connections/health/receive
Content-Type: application/json
Authorization: Bearer <connection-delivery-token>
```

The body is the raw JSON export. No query parameters or multipart wrapping are
needed. This endpoint always receives ongoing data and requests an import. Its
response contains `artifact_id`, `new`, and `operation_id` (or `already_imported`
for an export already loaded with the current parser and options). A 201/200
acknowledges receipt and submission, not completion of ingestion. Files and
History show the import outcome; the page also shows when the last export was
received, through either upload or automatic delivery. Uploads are capped at
512 MiB. Identical exports reuse the retained artifact; an already successful
import is not repeated.

Delivery tokens only authorize their own connection's receive endpoint. They
cannot read history, edit configuration, import historical files, or replay data.
Tokens are shown once, stored only as hashes, and can be replaced or revoked on
the connection's **Settings → Data delivery**. Replacing a token immediately invalidates the previous one;
removing a connection revokes its token. The installation-wide API token remains
for administration and the older artifact API. Receiving files does not require
`MIROIR_SECRET_KEY` when no source credentials need decrypting.

The phone must be able to reach the URL. This installation uses an internal
gateway, so use the home network or VPN. n8n can use the same HTTP endpoint as an
interim sender without installing the CLI or sharing the artifact directory.
Confirm a real receipt and successful import before disabling the old n8n workflow
or changing legacy dataset bindings (decision 009). The CLI's `submit` only
registers a file; its `import` command targets local DuckDB.

This change upgrades metadata to version 4 to add hashed delivery tokens and
separate first/latest receipt times. Identical repeated exports advance the latest
receipt time without changing the artifact's first receipt or re-importing it. Release
the server, worker, and flow-run image together: older runtimes refuse the new
schema version. Phone delivery and production cutover still need a real-device
check; synthetic tests alone do not establish those.

The Prefect flow `src/pipelines/miroir.py:sync_connection` uses the same runner
against Postgres and runs dbt for connections whose dataset matches its source
binding. Miroir manages its deployments through plan/apply; `prefect.yaml` holds
the legacy deployments only.

Configuration version 1 contains a miroir ID, connections, and optional deployment
settings and service links. Each
connection has an integration, validated settings, credential references, and an
optional explicit pipeline/dataset binding, resource selection (`resources`: a
subset of the integration's declared resources; unselected resources keep their
stored state and rows), analysis settings (`analysis.timezone`,
`analysis.parameters` validated by the analysis, e.g. GitHub `author_emails`),
schedule, and dashboard links.
Credentials use `provider: "miroir"` for the built-in encrypted store without a
key, or `provider: "env"` / `"fnox"` with a `key` naming the external value. Inline
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

Resource selection, configurable destinations, machine-local overlays, and stored
immutable revision snapshots remain outside this schema. Unsupported fields fail validation rather than being ignored.
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
uv run --locked ruff check src/core src/integrations src/analyses src/cli src/pipelines/miroir.py tests
uv run --locked ruff format --check src/core src/integrations src/analyses src/cli src/pipelines/miroir.py tests
uv run --locked mypy --follow-imports=silent src/core src/integrations src/analyses src/cli src/pipelines/miroir.py
```

## How to continue this PR

For every slice, append a dated entry to the implementation log describing the
problem, changed behavior, validation commands/results, limitations, and next step.
Record architectural choices in the numbered decision log; supersede old decisions
explicitly rather than silently erasing their rationale. Update roadmap status
when its completion criteria are actually met. Keep production cutover a distinct,
reviewable step with its own baseline comparison and recovery procedure.
