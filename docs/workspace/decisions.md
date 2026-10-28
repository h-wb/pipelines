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

Not yet: reconciling `running` attempts with Prefect (crash/cancel leaves them
`running`, reported as unverified), submitting requested operations to Prefect,
and per-resource partial-load tracking beyond dlt's own package resume.

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
