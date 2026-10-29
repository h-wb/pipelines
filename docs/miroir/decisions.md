# Workspace design decisions

## 001 — Versioned JSON configuration with strict typed validation

Status: accepted for the first slice, 2026-10-28.

Use one explicit JSON file, `schema_version: 1`, and Pydantic 2 models. JSON is
available on all supported Python versions and is straightforward for a later API
or configuration editor. TOML offers comments but needs a reader dependency on
Python 3.10; YAML has a larger parsing surface. Neither is necessary for this slice.
Revisit format ergonomics before building write/edit operations, with an explicit
migration if the persisted format changes.

Reject unknown fields and type coercion; validate integration settings against the
registry. Credentials are named references only. Diagnostics omit input values,
unknown field names, and user-supplied credential names so malformed configuration
cannot print a pasted token. CLI summaries omit account settings and reference keys.

The initial reader has no implicit environment overrides, file merging, secret
lookup, persistence, or network access. A future resolver can implement env/fnox
references inside a connection's execution boundary. Schedules/destinations/overlays
will be added with their concrete operations, not guessed in advance.

## 002 — Explicit identities and opt-in legacy bindings

Status: accepted for the first slice, 2026-10-28.

Workspace and connection IDs use lowercase letters, digits, and underscores, up to
24 characters. Defaults derive `ws_<workspace-ID-length>_<workspace-ID>_<connection-ID>`
for both dlt pipeline and dataset names. The length prefix prevents ambiguous names
such as workspace `a_b` plus connection `c` versus workspace `a` plus connection `b_c`.
Names remain within Postgres's 63-byte identifier limit. They are not derived from
usernames or tokens, so credential changes need not change state identity.

An explicit binding preserves legacy pipeline/dataset names. Validation rejects
colliding pipeline or dataset names within the workspace, including collisions
between explicit and generated bindings. The first product has one destination per
workspace; cross-workspace ownership of manually supplied bindings remains a future
execution-time check. Configuration validation does not authorize adopting live data.

A canonical content hash identifies normalized desired configuration. Object key
order, whitespace, and connection list order do not affect the hash; settings,
reference changes, and bindings do. Runtime revision storage and deeply immutable
execution snapshots are not implemented by the reader's frozen model objects.

## 003 — Small bundled registry and lazy source adapters

Status: accepted for the first slice, 2026-10-28.

Start with ListenBrainz and Apple Health in an explicit registry. Definitions expose
settings schemas, credential names, resources/keys, input modes, and underlying
capabilities. Do not load arbitrary modules from configuration or implement a plugin
installer yet. The CLI must be usable without importing dlt or Prefect.

`src/integrations/sources.py` adapts a validated connection to the existing dlt source
with explicit settings and already-resolved credentials. It does not mutate process
environment variables or resolve secrets. Keep existing source/flow implementations
intact to preserve production behavior. Execution, destination bindings, concurrency,
and credential resolution belong to the next layer.

Apple Health's current directory scanner remains a compatibility path. Explicit
artifact import will use a dedicated parser with origin/cutoff metadata and will
bypass modification-time scanning. (Implemented by decision 008.) Native XML and generic CSV are separate format
implementations, not implied by the integration's presence in the catalog.

## 004 — Lock application dependencies; validate outside the worker

Status: local validation accepted; production runtime selection pending, 2026-10-28.

Commit `uv.lock` and use `uv sync --locked` in a separate Python 3.12 environment for
validation. Keep the package's Prefect compatibility range while locking this tested
application environment. Do not install or upgrade dependencies in the shared worker
as part of development. The observed worker image uses Python 3.12 / Prefect 3.9.0.

Use a small test group for DuckDB-backed synthetic integration tests and stdlib
unittest for the suite. Add Pydantic as an explicit application dependency instead
of relying on Prefect's transitive dependency. Remove the obsolete `dlt[workspace]`
extra, which the resolved dlt 1.31.0 does not provide; retain marimo separately.

A local venv is evidence of dependency/source compatibility, not an actual Prefect
worker isolation solution. Validate the process/container execution mechanism,
mounts, secret injection, and restart behavior separately before production changes.

## 005 — Operation and attempt records in a small SQLAlchemy metadata store

Status: accepted for the execution slice, 2026-10-28.

An **operation** is one requested or observed sync of one connection; an **attempt**
is one execution of it. A Prefect flow run maps to exactly one operation (unique
`backend_run_id`), so Prefect retries become attempts 2, 3, … rather than new
operations. A CLI-requested operation is created first and adopted by the flow run
that receives its `operation_id`; adopting it from another connection or binding it
to a second backend run is rejected. Schedules and n8n triggers pass no operation
ID and register their own on entry, so every execution path is visible.

Each attempt records ingestion (`running|succeeded|failed|blocked`) and
transformation (`pending|succeeded|failed|skipped|not_run`) separately, plus dlt
load IDs and a redacted error (credential values replaced with `***`, length
capped). Status is a projection of these records; Prefect keeps execution logs and
dlt keeps cursors. Nothing here copies dlt state.

Store: SQLAlchemy Core, one code path for SQLite (local/tests) and PostgreSQL
(schema `workspace_meta` in the existing instance). `schema_info` holds a version;
`migrate()` creates version 1 and refuses unknown versions. Real migrations
(Alembic or hand-written) come with the first schema change. SQLAlchemy is now an
explicit dependency; it was previously only transitive through Prefect.

Not yet: per-resource partial-load tracking beyond dlt's own package resume.
Reconciling `running` attempts and submitting requested operations to Prefect
are now built (decision 013).

## 006 — Runner ordering: register, lock, load, record, model

Status: accepted for the execution slice, 2026-10-28.

`src/core/runner.py:run_connection` is the only execution path:

1. Register or adopt the operation. If the metadata store is unavailable, nothing
   loads: new executions require registration.
2. Take the connection lock (keyed by dlt pipeline name). If held, record a
   `blocked` attempt and raise `ConnectionBusy`; do not queue or wait. PostgreSQL
   uses a session advisory lock; SQLite uses `flock` on a file beside the database.
   Both release when the holding process dies. Legacy flows do not take this lock,
   so a workspace connection bound to a legacy pipeline name must not run alongside
   its legacy deployment. Cutover disables the legacy deployment first.
3. Resolve credentials for this connection only, then run dlt.
4. Record load success. If that write fails, log it and return `recorded=False`;
   never report a committed load as failed.
5. Run dbt only when the connection writes the dataset the dbt source reads
   (`ModelBinding` in the registry); otherwise `skipped` with the reason. A dbt
   failure is recorded and leaves ingestion `succeeded` and analysis `stale`. This
   replaces the legacy `refresh_models` behavior of logging and moving on.

The dbt selector stays `source:<name>+`, matching the legacy flows, so it still
includes crossover models that need other sources. Narrowing to self-contained
models is milestone 2 work tied to the dbt project, not to the runner.

## 007 — Prefect entry point exists but is not deployed

Status: accepted, 2026-10-28; deployment blocked on runtime decisions.

`src/pipelines/workspace.py:sync_connection` wraps the runner with Postgres, dbt,
and the flow run ID. It is deliberately absent from `prefect.yaml`. Deploying it
requires, in order: a decision on worker isolation (home-ops change or existing
work-pool facility), worker-side credential resolution (fnox on the worker or
explicit injection; `resolve_credentials` supports both `env` and `fnox`), a
workspace configuration and `WORKSPACE_METADATA_URL` on the worker, and an isolated
Postgres schema check. `run` in the CLI targets local DuckDB only until then.

## 008 — Content-addressed artifacts and explicit, versioned imports

Status: accepted for the import slice, 2026-10-28.

**Receipt and import are separate records.** An artifact records bytes received
for one connection: SHA-256, size, input format, role, original name, storage path,
retention (`keep`), and received time. Identity is (workspace, connection, SHA-256):
identical bytes from two accounts are two artifacts. Imports are operations
(`kind` `import` or `replay`) carrying `artifact_id`, `parser_version`, and
`options`, executed by the shared runner under the connection lock.

**Storage.** Files are copied to a temporary name in the destination directory,
fsynced, and renamed to `<root>/<workspace>/<connection>/<sha256>` with mode 0440.
An interrupted copy is deleted and never registered. Validation runs before the
copy, so invalid files are neither stored nor recorded. Import re-hashes the
stored copy and refuses a missing or changed file. Local disk only for now.

**Role is metadata, not a path.** For Apple Health, `historical`/`ongoing` replace
the `/manual/` path test, with the cutoff still taken from connection settings.
Both paths share `select_rows` in `src/sources/apple_health.py`, so the rule is
defined once. The artifact source has no incremental cursor and does not touch the
legacy scanner's `filesystem` state, so explicit imports and the compatibility
scanner can coexist on one connection.

**When an artifact counts as imported.** It counts as imported when an attempt
succeeded with the same parser version and the same options (role and cutoff).
Bumping `PARSER_VERSIONS` or changing the cutoff makes the next plain `import` load
it again. `--replay` always loads. Replays are merge-only: they cannot remove rows
an older parser produced wrongly. That needs the scoped reconciliation the plan
describes, not yet built. If the load committed but its completion was not recorded
(crash window), the artifact is not marked imported and the next import reloads it;
merge keys make that safe for the current Apple Health resources.

**Schema version 2** adds `artifacts` and the three operation columns. `migrate()`
upgrades version 1 in place (create missing tables, `ALTER TABLE ... ADD COLUMN`,
bump the version) in one transaction, and refuses versions it does not know.

Not yet: archive formats and resource limits, retention/deletion operations, a
Prefect entry point for imports, migrating the n8n drop folder to `submit`,
transformation-only retries, and native Apple Health XML or other formats.

## 009 — n8n is a temporary receiver for Health Auto Export

Status: accepted, 2026-10-28.

Today n8n has one job. Workflow "Apple Health → Files: Ingest Exports" receives
Health Auto Export's webhook POSTs, writes each body unchanged to the NAS share
mounted on the worker, then starts `load_apple_health` through the Prefect API. It
exists only because nothing else in the stack accepts HTTP uploads.

The workspace HTTP API (milestone 4) replaces it with an upload endpoint for the
connection's artifacts. That endpoint is the same `submit` (and optional import)
path as the CLI and web form, so phone pushes become one more acquisition path for
the artifact inbox. This removes a service from the health data path and the
shared-folder write access. Every receipt is also recorded, so a silent stop like
the `N8N_RESTRICT_FILE_ACCESS_TO` separator bug (no writes from 2026-06-07 to
2026-09-22) shows up as "no export received since …" instead of going unnoticed.

Until the workspace server is deployed, the existing workflow and legacy
deployment keep running unchanged. Cutover: point Health Auto Export at the
workspace endpoint, confirm receipts and imports, then disable the n8n workflow
and the legacy Apple Health deployment. Do not run both against the same
connection's dataset (decision 006). n8n is not a dependency of the workspace
design; other automations in n8n are out of scope.

## 010 — Small server-rendered interface over shared services

Status: accepted for milestone 4, 2026-10-28.

**Stack.** FastAPI, Jinja2 templates, plain HTML forms, and a few lines of CSS. No
JavaScript build and no client framework. Every web dependency was already in the
lockfile through Prefect; they are now declared explicitly. A richer frontend can
replace the templates later, because the JSON API exposes the same operations.

**One service layer.** `src/core/service.py:WorkspaceService` is what the HTTP API
and web pages call: overview, connection detail, request sync, submit file,
request import, read/save configuration. Execution goes through an `Executor`:
`SubprocessExecutor` (server: one `pipelines-workspace execute` process per
operation, so a crash affects one operation) and `InlineExecutor` (tests). A
Prefect executor that creates flow runs of `sync_connection` replaces the
subprocess one when the runtime decisions land (decision 007).

**Request, then execute.** The runner now splits `request_sync`/`request_import`
(record the operation, visible as requested) from `execute_operation` (run one
attempt). `run_connection` and `import_artifact` remain as request-plus-execute.
Requested executions wait up to an hour for the connection lock instead of being
blocked. A demo push of two exports at once showed the second one blocked and
never retried before this change.

**Configuration identity.**
- Edit conflicts compare a SHA-256 of the exact file bytes (`file_revision`),
  under a lock, with atomic replacement. Any change by someone else, including
  formatting, is a conflict. An invalid file can still be repaired.
- Each connection has its own revision (its normalized configuration plus the
  workspace ID). Operations record it. A request runs only if its connection is
  unchanged; otherwise its attempt is `blocked`. Edits to one connection do not
  block another's queue.
- "Saved vs applied" compares the connection's revision with that of its latest
  executed attempt (`applied`, `saved_not_applied`, `never_run`).
- `links` (Prefect UI, Metabase) and per-connection `dashboards` are optional,
  additive schema fields. They do not affect execution revisions.

**Auth.** Single user, one token from `WORKSPACE_API_TOKEN` (16+ characters,
required to start). API clients send it as a bearer token. Browsers exchange it
once for an `HttpOnly`, `SameSite=Strict` cookie holding an HMAC derived from the
token, never the token itself. Cookie-authenticated POSTs with a foreign `Origin`
are refused. Redirect notices are fixed codes, so nothing from the URL is reflected.
Jinja autoescaping covers user-supplied names. The server binds to 127.0.0.1 by
default; put TLS in front of it before exposing it.

**Analysis states.** `not_modelled` is new: the connection's dataset is not one
the dbt models read. Previously this showed as `stale`, which implied it would
catch up.

Not yet: a structured config editor (JSON textarea for now), multi-user accounts,
exercising the flow against this installation (expired credentials, outages) on
the test setup. The Prefect executor and reconciliation are now built
(decision 013).

## 011 — Connection secrets: encrypted in the workspace, pluggable backends

Status: accepted, 2026-10-28. Replaces the idea, discussed but never built, of
using fnox as the product's secret store.

This follows the established pattern of comparable tools. n8n
(`N8N_ENCRYPTION_KEY`) and Airflow (`fernet_key`, plus pluggable secrets backends)
store connection secrets encrypted in their own database under one master key and
offer external managers as alternatives. Airbyte stores only references in
connector configuration. The goal is that another person can add a connection by
pasting a token, as in those tools, without running a secret manager.

**Two kinds of secret.**
- *Connection secrets* (a person's ListenBrainz or GitHub token) are entered in
  the UI or CLI and stored by the workspace. Only the job that runs the
  connection decrypts them, just before creating the source.
- *Installation secrets* (the master key, Postgres roles, Prefect and Metabase
  API credentials) are provided by whoever deploys, as environment variables from
  a Kubernetes Secret, a `.env` file, or fnox/mise locally (twelve-factor). The
  workspace never stores these.

**Built-in store.** A `secrets` table in the metadata database holds one ciphertext
per (workspace, connection, credential name), encrypted with `cryptography`'s
Fernet (AES-128-CBC with HMAC-SHA256, the scheme Airflow uses). No custom
cryptography. The key comes from `WORKSPACE_SECRET_KEY`; a comma-separated list
enables rotation (first key encrypts, all decrypt; `rotate-secrets` re-encrypts).
The key never goes in the database or the configuration file. Back it up
separately: without it, stored secrets are unrecoverable.

**References, not values.** Configuration holds `{"provider": "workspace"}` for a
built-in secret, whose identity is implied by connection and credential name.
Secrets therefore cannot be shared across connections by mistake. `env` and
`fnox` references with explicit keys remain supported as external backends.
Vault/OpenBao, Kubernetes, or cloud secret managers can be added behind the same
interface (the Airflow secrets-backend model). Nothing returns a stored value:
UI and API show only "set / updated at".

**Integrations declare credentials** as named fields with a label and help text.
They are not `SecretStr` fields in the settings model, as first sketched. Settings
are persisted to the configuration file; keeping credentials in a separate
structure makes "never persisted there" structural rather than a filtering rule.
Resolved values are passed in memory only.

**Check and health.** An integration may declare a check: ListenBrainz calls
`/1/validate-token` and confirms the account name, and Apple Health confirms its
folder is readable. A failed check, or an HTTP 401/403 during a run, makes the
connection's credential status `rejected` until a later check or run succeeds.
This is a status of its own, with a "Replace" action, instead of a generic
failed run.

**Lifecycle.** Adding a connection validates the configuration, stores its
secrets, then saves the configuration (revision-checked). If the save fails, the
just-stored secrets are removed. Removing a connection deletes its configuration
entry and its secrets. Loaded data, artifacts, and history are kept; purging them
is a separate operation, per the plan.

**Each component gets only what it needs.** The server and worker get the master
key and metadata database. dlt gets a Postgres writer role, dbt a transformer
role, and Metabase a read-only role. Only the worker running a connection
decrypts that connection's secrets.

Not yet: OAuth flows (consent, refresh-token rotation), external backends beyond
env/fnox, and per-user key separation for a hosted multi-user version.

## 012 — Keep Prefect; run each flow in its own Kubernetes Job from a versioned image

Status: accepted, 2026-10-28. Settles runtime question 1 of decision 007.

**Prefect stays** as the cluster orchestrator. It is deployed, Python-native,
works with dlt, and its UI is the "execution details" page the workspace links
to. Dagster's asset, freshness, and lineage model overlaps the workspace itself
(it would duplicate or replace the product layer). Airflow is heavier.
Kestra and Windmill are less Python-native. Bare CronJobs would mean rebuilding
retries, run history, and logs. The failures so far (a dependency conflict that
broke the worker, the server's SQLAlchemy pin) come from installing code into one
shared worker environment. Changing orchestrator would not fix them; isolation
does.

Prefect is also the single-machine orchestrator (decision 013): a `prefect server`
on SQLite plus a process worker, bundled for installation. The workspace does not
build its own scheduler.

**Runtime.** Observed: one long-lived process worker (`local-pool`) that clones
`main` and `uv pip install`s into its own environment on each run. All flows
share its 6 Gi limit, and it has no permissions to create Jobs. Replacement:

- One image, `ghcr.io/h-wb/pipelines`, built in CI from `uv.lock` (Python 3.12,
  Prefect matching the server), following the `sento` image workflow. The same
  image runs flow runs, the Kubernetes worker, and later the workspace server.
  Nothing is installed at run time. The image digest identifies code and
  dependencies (`PIPELINES_CODE_REVISION` is set at build time).
- A Kubernetes work pool (`workspace-k8s`). Each flow run is its own Job with its
  own resource limits; a crash affects one run.
- home-ops: a second worker controller running the image
  (`prefect worker start --type kubernetes`), a namespaced Role to manage Jobs,
  Pods, and logs, an ExternalSecret (Bitwarden Secrets Manager, the cluster's
  existing source) with the installation secrets from decision 011, and the
  existing NFS export mounted for artifacts.
- The legacy deployments stay on `local-pool` until their connection is cut over;
  both pools run side by side.

## 013 — Prefect is the orchestrator; the workspace only reconciles with it

Status: accepted, 2026-10-28. An earlier draft (same day, never built) also
specified a workspace-owned "local backend" with its own cron loop for
installations without Prefect. That draft was dropped because it reinvents what
Prefect already does on one machine: a SQLite-backed `prefect server` and a
process worker give the same scheduling, retries, run history, and logs as the
cluster. One orchestrator in every installation means less code and one
behaviour to reason about.

The workspace owns the **desired** state: connections and their optional
`schedule` (`{"cron": "...", "timezone": "..."}`, not part of the execution
revision). Prefect owns **execution**. The glue does three things, all through
Prefect's own APIs:

1. **Deployments from configuration.** Each connection gets one deployment of
   `sync_connection` (`sync-connection/<workspace>-<connection>`). Its parameters
   (`connection_id`, config path), schedule, work pool, image, and a concurrency
   limit of 1 (Prefect queues overlapping runs of one connection) all come from
   the workspace. `plan` lists creates, updates, and removals against what Prefect
   has, so hand edits in Prefect show as drift. `apply` performs them. A
   deployment per connection, rather than one deployment with per-schedule
   parameters (also supported by Prefect 3.9), keeps per-connection pause,
   concurrency, and run history in the Prefect UI.
2. **Submit.** A request becomes `run_deployment(..., timeout=0,
   idempotency_key=<operation ID>)`, so retrying an ambiguous submission returns
   the same flow run. The flow run ID is bound to the operation; the flow adopts
   it on entry (decision 005). Scheduled runs register their own operation.
3. **Observe.** Attempts still `running` (and submitted operations with no attempt)
   are compared with their flow run's state. Crashed becomes `lost` and cancelled
   becomes `cancelled`, both kept apart from `failed`. Pending or running is left
   alone.

The subprocess executor remains for development and tests only (manual requests,
no schedules). It is not a product mode.

## 014 — The product is Miroir; "workspace" is retired

Status: accepted, 2026-10-29.

Until now "workspace" was both the working title of the product ("the personal
data workspace") and the name of everything inside it: the configuration unit,
the environment variables, the metadata schema, the CLI. The product is
**Miroir** — one installation mirrors one person's data — and the old name is
gone entirely rather than half-kept:

- Python (`MiroirConfig`, `MiroirService`, `load_miroir`, `miroir_id`, …),
  the CLI binary (`pipelines-miroir`, config file `miroir.json`, local state
  in `.miroir/`), the environment (`MIROIR_*`), the metadata schema
  (`miroir_meta`), the built-in secrets provider value (`{"provider":
  "miroir"}`), the Prefect work pool (`miroir-k8s`), and the cluster
  resources in home-ops.
- One unit of configuration is a miroir: one `miroir.json`, one owner, its
  connections inside. The name no longer means two things.

Earlier entries in this log keep the wording of their time; paths moved with
`docs/workspace/` → `docs/miroir/`.

## 015 — The metadata store gets its own Postgres, not Metabase's

Status: accepted, 2026-10-29.

The metadata store is Miroir's control plane (operations, attempts, artifact
registry, connection checks, encrypted connection secrets). It ran in
postgres-metabase because that instance already existed, but that couples a BI
database's lifecycle to the pipeline bookkeeping and widens the blast radius of
either. Following home-ops' standard pattern, Miroir gets its own CloudNativePG
cluster — `postgres-miroir` in the automation namespace, bootstrapped with the
`cnpg/initdb` component, backed up to Garage via Barman like every other
cluster — and the job template reads its credentials from the generated
`postgres-miroir-app` secret.

The **warehouse data** (dlt datasets, dbt models, Metabase dashboards) stays on
postgres-metabase; moving it would be a data migration with dashboard
re-pointing and buys nothing. The destination credentials keep coming from
`postgres-metabase-app`.

`MIROIR_METADATA__*` (host, port, database, username, password) compose the
metadata URL, so the database password still exists nowhere outside CNPG.

## 016 — Execution settings live in the configuration, not the writer's environment

Status: accepted, 2026-10-29.

Until now the deployment job's work pool, image, and job variables came from
each writer's environment (`MIROIR_PREFECT_POOL`, `MIROIR_IMAGE`,
`MIROIR_PREFECT_JOB_VARIABLES`). With two writers — the interface and the CLI —
that made desired state writer-dependent: disagreeing environments would apply
different deployments and fight over drift forever.

The configuration now carries a `deployment` section (`work_pool` required;
`image` and `job_variables` optional), authoritative per field. The
environment variables remain as fallback defaults for fields the section omits
and for a first setup, and either source enables the Prefect integration. Like
schedules, the section is not part of a connection's execution revision.
Consequences: any writer produces the same plan; apply is writer-independent;
and on the cluster the image pin (a digest, never a mutable tag — the
registry mirror serves those stale) is one config edit instead of three
environment pins. `MIROIR_PREFECT_CONFIG_PATH` stays an environment concern:
where flow runs find the configuration genuinely differs between the operator's
machine and the Job's mount.

## 017 — File delivery is independent of parsing; connection-scoped receive tokens

Status: accepted, 2026-10-29.

An Apple Health connection accepts Health Auto Export JSON without a server folder.
Browser uploads and automatic HTTP delivery enter the same retained-artifact and
import pipeline. `data_dir` becomes optional; when present it enables the existing
directory scanner as an advanced compatibility mode. Checks for a receiving-only
connection describe readiness to accept exports, not successful access to a phone.
Existing folder configurations and their execution revisions remain valid.

The connection page offers file upload and automatic-delivery setup together.
`POST /api/connections/{id}/receive` accepts raw JSON, retains it as an ongoing
artifact, and requests an import. Receipt acknowledges storage/submission, not
completed ingestion. Identical exports retain the existing duplicate/import rules.
The existing administrator artifact API remains available for historical imports.

Each connection can have one receive-only bearer token: generated randomly, shown
once on creation/replacement, stored only as a SHA-256 digest in metadata schema
version 4. It authorizes only that connection's receive endpoint, never configuration,
history, credentials, replay, or another connection. Replacing/revoking it invalidates
the previous token; removing the connection revokes it. No installation master key
is needed to receive files. Setup instructions show the endpoint and headers and
explain that the phone must be able to reach Miroir. Native Apple XML, arbitrary
JSON webhooks, and automatic discovery of input formats remain separate work.
Version 4 also preserves first and latest receipt times separately, so repeated
identical exports update delivery health without changing artifact identity or
re-importing successful data.
