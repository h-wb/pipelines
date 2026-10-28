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
bypass modification-time scanning. Native XML and generic CSV are separate format
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
