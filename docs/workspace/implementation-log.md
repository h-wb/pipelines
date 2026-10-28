# Implementation log

## 2026-10-28 — Planning and baseline

- Preserved the reviewed roadmap and its acquisition/import separation, staged
  delivery, state ownership, and migration constraints in `docs/product-plan.md`.
- Created `feat/personal-data-workspace` for incremental work in a draft PR.
- Read the existing reference flows/sources, dbt source bindings, deployment
  configuration, source pages, and operations runbook. Recorded repository facts
  and separately identified live worker image/mount observations in `baseline.md`.
- No production flow was triggered and no deployment configuration was changed.

## 2026-10-28 — Configuration, registry, CLI, and source adapters

Implemented:

- Strict versioned JSON workspace/connection configuration and named credential
  references. Unknown fields, invalid dates/types, duplicate JSON keys, and conflicting
  pipeline/dataset bindings fail validation.
- Stable generated bindings, explicit legacy mappings, and normalized revision hashes.
- Bundled ListenBrainz / Apple Health definitions and lazy adapters that pass settings
  and resolved credentials directly to the existing sources.
- Read-only `pipelines-workspace integrations|validate|connections` commands, JSON
  output, and safe diagnostics. Local configuration/input paths are gitignored.
- Committed dependency locking, explicit Pydantic dependency, and a DuckDB test group.

Validation:

- Installed from the lockfile into a separate Python 3.12 environment.
- `RUNTIME__DLTHUB_TELEMETRY=false <isolated-python> -m unittest discover -v`:
  13 tests passed. These cover malformed/secret-bearing configuration, binding
  collisions, CLI runtime/network isolation, real dlt source loading, repeat imports,
  independent API accounts, and restoring committed state into a fresh local directory.
- Source tests used synthetic files and intercepted HTTP, fake tokens, and a temporary
  DuckDB database. The existing source and production flow files were not edited.
- Final checks after `uv sync --locked --dev` (repo `.venv`, Python 3.14.7, ruff 0.17.3,
  mypy 2.4.0): `ruff check` and `ruff format --check` clean on the new packages and
  tests; `mypy --follow-imports=silent src/core src/integrations src/cli` reports no
  issues (needed `explicit_package_bases`); the unittest suite passes there too.
- CLI smoke: `validate` on `examples/workspace.json` exits 0 with two connections;
  a missing config file exits 2 with a value-free diagnostic. `uv build --wheel`
  includes `src/core`, `src/integrations`, `src/cli`, and the `pipelines-workspace`
  entry point.

Milestone status:

| Milestone | Status |
| --- | --- |
| 0: baseline/runtime boundary | Reference baseline and local dependency/source proof done; actual work-pool, secret, storage, and Postgres proof pending |
| 1: contracts/registry | Initial reader, explicit catalog, source adapters, CLI, and contract tests implemented |
| 2: execution/status | Not implemented; no operation metadata, secret resolver, scheduler reconciliation, or execution command |
| 3: import inbox | Not implemented; current directory import is compatibility-tested, not an artifact registry/replay implementation |
| 4+: interface/migration/analyses | Not started |

Next slice:

1. Establish the runnable Prefect isolation path and worker credential/storage access.
2. Define operation/attempt records and the worker entry point shared by schedules,
   CLI requests, and direct/n8n triggers.
3. Add a supported API sync in an isolated destination with truthful load/build status.
4. Then implement explicit artifact registration/parsing, replacing path/mtime assumptions
   for new manual imports while keeping the legacy scanner available during migration.

## 2026-10-28 — Operation records, shared runner, and status

Codex's session ended at its usage limit before the slice-1 checks finished; Claude
completed them, opened draft PR #2, then implemented this slice.

Implemented (decisions 005–007):

- `src/core/operations.py`: SQLite/PostgreSQL metadata store with versioned schema,
  operations, attempts, per-connection locks, and a status projection.
- `src/core/runner.py`: one execution path that registers/adopts the operation,
  locks the connection, loads with dlt, and records ingestion and dbt separately.
- `src/core/secrets.py`: per-connection `env`/`fnox` resolution, value-free errors.
- `ModelBinding` in the registry ties each integration to the dbt source and the
  single dataset its models read.
- `pipelines-workspace status` and `run <connection> --duckdb PATH` (local only).
- `src/pipelines/workspace.py:sync_connection` Prefect flow, not deployed.
- Explicit `sqlalchemy>=2.0,<3` dependency.

Validation:

- `ruff check`, `ruff format --check`, and `mypy` clean on the new packages, the
  flow module, and tests.
- 24 tests pass in the repo `.venv` (Python 3.14.7) and in an isolated
  `uv sync --locked --python 3.12 --no-dev --group test` environment (3.12.15).
  The new tests cover: generated datasets skip models and read as stale; dbt
  failure keeps the load and marks analysis stale; ingestion failure redacts the
  token; Prefect retries add attempts to one operation; requested operations are
  adopted and cannot be rebound or adopted cross-connection; the same connection is
  locked while another runs; a metadata outage after loading returns
  `recorded=False`; no load happens without registration; records survive reopening
  and unknown schema versions are refused; CLI `run` then `status` on a file import.
- The flow module imports under Python 3.12 / Prefect 3.9.0 with parameters
  `connection_id`, `operation_id`, `config_path`.
- Not run: anything against Postgres (advisory locks, `workspace_meta` schema), a
  real Prefect run, or a live ListenBrainz/fnox run.

Milestone status:

| Milestone | Status |
| --- | --- |
| 0: baseline/runtime boundary | Unchanged: work-pool isolation, worker secrets, storage, and Postgres proof pending |
| 1: contracts/registry | Done for the two reference integrations |
| 2: execution/status | Operation/attempt model, worker entry point, locking, separate dbt outcome, and status done locally; Prefect submission, reconciliation of running attempts, schedule plan/apply, self-contained model selection, and Postgres/worker proof pending |
| 3: import inbox | Not implemented |
| 4+: interface/migration/analyses | Not started |

Next slice:

1. Runtime decisions (decision 007), then an isolated Postgres schema run of the
   store and runner, and one `sync_connection` deployment against a test dataset.
2. `request` command that creates an operation and submits it to that deployment;
   `status --refresh` reconciling unverified attempts with Prefect flow run state.
3. Explicit artifact import for Apple Health (milestone 3), which can proceed
   locally in parallel.

## 2026-10-28 — Artifact submission and explicit import (milestone 3, local)

Implemented (decision 008):

- `src/integrations/formats.py`: stdlib format validation for Health Auto Export
  JSON, plus parser versions.
- `src/core/artifacts.py`: atomic, content-addressed file receipt and submission.
- Metadata schema version 2 (`artifacts` table, import fields on operations) with an
  in-place upgrade from version 1.
- `import_artifact` in the runner. Syncs and imports now share one locked
  execution path that takes a source factory.
- `apple_health_artifact` source and the shared `select_rows` rule. Legacy scanner
  behavior is unchanged (its test still passes).
- CLI: `submit`, `import [--replay]`, `artifacts`.

Validation:

- Lint, format, and mypy clean. 36 tests pass on Python 3.14.7 (with
  `-W error::ResourceWarning`) and in an isolated Python 3.12.15 environment.
- New tests: cutoff/role selection; invalid files rejected before storage;
  per-connection identity and role conflicts; repeat skipped, replay merge-safe,
  overlapping exports merged; changed contents with an unchanged mtime skipped by the
  legacy scanner but imported explicitly; failed imports retried and never marked
  imported; parser version bump reimports; crash window after commit reimports
  without duplicates; tampered stored copy refused; interrupted copy leaves no
  file; version 1 metadata upgraded in place with data kept; CLI submit/import/list.
- Not run: Postgres, Prefect, real exports.

Milestone status:

| Milestone | Status |
| --- | --- |
| 0: baseline/runtime boundary | Unchanged; runtime decisions pending |
| 1: contracts/registry | Done for the two reference integrations |
| 2: execution/status | Local parts done; Prefect submission/reconciliation and worker proof pending |
| 3: import inbox | Submission, storage, validation, import, replay, and inspection done locally for Apple Health; worker entry point, n8n migration, retention operations, and reconciliation-based replay pending |
| 4+: interface/migration/analyses | Not started |

Next slice:

1. Runtime decisions (decision 007), then Postgres and one test deployment.
2. A Prefect entry point for imports and a path for n8n to `submit` instead of
   dropping files into the scanned directory.
3. Milestone 4 can start against the local store: the CLI operations are the
   application services a small interface would call.

## 2026-10-28 — Workspace interface and HTTP API (milestone 4, local)

Implemented (decisions 009, 010):

- Decision 009: n8n stays as the Health Auto Export receiver only until the workspace
  server is deployed; the artifact upload endpoint replaces it.
- Runner split into request and execute, a per-connection revision check, and
  lock waiting for queued requests.
- Revision-checked, atomic configuration saves. Optional `links` and per-connection
  `dashboards` in the schema.
- `WorkspaceService` with subprocess and inline executors.
- `src/web`: token auth, JSON API, and server-rendered pages for overview,
  connection detail (sync/scan, upload, import/replay, history), and configuration.
- CLI: `serve` and `execute`. Explicit `fastapi`, `uvicorn`, `jinja2`, and
  `python-multipart` dependencies; `httpx` in the test group. No lockfile version
  changed.

Validation:

- Lint, format, and mypy clean. 46 tests pass on Python 3.14.7 (with
  `-W error::ResourceWarning`) and in an isolated Python 3.12.15 environment. The
  wheel includes `src/web` and its templates.
- New web tests: auth on every route except login/health; the session cookie is not
  the token; cross-origin cookie posts refused; phone-style raw upload stores and
  imports once, then reports already imported; invalid, oversized, wrong-role, and
  wrong-connection uploads rejected with nothing kept; form upload escapes file
  names; replay; sync then saved/applied states; a queued request blocked after its
  connection is edited; config conflicts (API and form) and invalid saves leave the
  file untouched; Prefect run links; the real subprocess executor.
- Manual demo with `serve` against /tmp files: pushed two Health exports at once
  through the API. The first run had the second import `blocked`, which led to lock
  waiting. Rerun: both imports succeeded, with 3 rows as expected. Checked the
  overview, connection, and configuration pages in a browser. That review added the
  `not_modelled` state, file names in history, the "Scan folder" label, and no
  navigation on the login page.
- Not run: anything against this installation (Postgres, Prefect, real ListenBrainz,
  Health Auto Export pushing to the endpoint).

Milestone status:

| Milestone | Status |
| --- | --- |
| 0: baseline/runtime boundary | Unchanged; runtime decisions pending |
| 1: contracts/registry | Done for the two reference integrations |
| 2: execution/status | Local parts done; Prefect executor/reconciliation and worker proof pending |
| 3: import inbox | Done locally; worker deployment and n8n cutover pending |
| 4: interface | Connect → sync/import → inspect → open analysis works locally for both reference integrations; test-setup exercise and deployment pending |
| 5+: migration/analyses | Not started |

Next:

1. The runtime decisions (decision 007) now gate everything: worker isolation,
   worker credentials, Postgres test schema. Then a Prefect executor and a deployed
   server, exercised against the test setup.
2. n8n cutover for Health Auto Export (decision 009).
3. Reassess scope from actual use, as the plan requires after milestone 4.
