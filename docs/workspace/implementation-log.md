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

## 2026-10-28 — Connection secrets and connection setup (decision 011)

Design discussion: credentials have to work for someone without a secret
manager, as in n8n or Airflow. Using fnox as the product's store was considered
and rejected (CLI dependency, file-based ciphertext; it remains an external
backend). The decision follows the n8n, Airflow, and Airbyte pattern.

Implemented:

- Metadata schema version 3: `secrets` (Fernet ciphertext per workspace,
  connection, and name), `connection_checks`, and `attempts.failure_kind`. In-place
  upgrade from versions 1 and 2.
- `src/core/secrets.py`: `SecretBox` (MultiFernet; key rotation), the built-in
  `SecretStore`, and `make_resolver` over workspace, env, and fnox backends.
  `{"provider": "workspace"}` references, scoped by connection.
- Integrations declare `CredentialField`s, titled settings, and a check: ListenBrainz
  `validate-token` plus account match; Apple Health folder readable.
- Run-time failure classification (`credentials_rejected` on 401/403,
  `credentials_missing`) and a credential status built from the latest check or run.
- Service: integrations catalog, create connection (validate, store secrets, save
  config, roll back secrets on failure), set credential, check, remove connection.
- Web: Add connection (forms generated from the registry), write-only credential
  rows with Replace/Test, removal with typed confirmation, credential status on
  the overview, and an empty-workspace state. `serve` creates an empty
  `workspace.json`. JSON API for the same operations.
- CLI: `generate-key`, `set-credential` (stdin or hidden prompt, never argv),
  `check`, `rotate-secrets`. The Prefect flow resolves built-in secrets when
  `WORKSPACE_SECRET_KEY` is set on the worker.
- Fixed in the existing ListenBrainz source: the paginator parsed error bodies
  before checking the HTTP status, so an expired token surfaced as
  `KeyError: 'payload'`. It now raises the HTTP error, which affects the legacy
  production flow too (successful responses unchanged).
- Redaction masks longer secrets first. `cryptography` declared explicitly (already locked through Prefect).

Validation:

- Lint, format, and mypy clean. 65 tests pass on Python 3.14.7 (with
  `-W error::ResourceWarning`) and in an isolated Python 3.12.15 environment.
- New tests: encryption round trip, wrong key, malformed key, and rotation; reference
  validation; config and every file on disk contain no secret value; scoping and
  deletion per connection; failed save rolls back secrets; a duplicate ID keeps the
  existing secrets; invalid settings store nothing; check ok, wrong account, and 401;
  a run refused with 401 sets status `rejected` from the run, then a replaced token
  and successful load restore `ok`; behaviour without a key; env references;
  external references cannot be replaced from the UI; web form never echoes secrets
  on error; API create/replace/check/remove never returns values; CLI
  `set-credential` reads stdin; `generate-key` produces a valid key.
- Manual: fresh server with no configuration (created empty), logged in, viewed
  the generated ListenBrainz form, added an Apple Health connection through the
  form (automatic check: folder readable). No real ListenBrainz token was used.

Next: unchanged, still gated by the runtime decisions (decision 007). Decision 011
narrows the worker's secret needs to `WORKSPACE_SECRET_KEY`, the metadata
database, and a Postgres writer role.

## 2026-10-28 — Prefect orchestrator: schedules, submission, reconciliation (decisions 012, 013)

Design discussion: keep Prefect (cluster and single machine) rather than build a
workspace-owned scheduler; a draft "local backend" with its own cron loop was
dropped because a SQLite-backed `prefect server` plus a process worker already
gives the same behaviour. The workspace owns desired state, Prefect owns
execution, and the workspace only reconciles (plan/apply deployments, submit,
observe). Decision 012 settles the runtime question: one versioned image, one
Kubernetes Job per flow run (not yet built — CI image and home-ops worker come
next).

Implemented:

- `src/adapters/prefect.py`: the orchestrator adapter. Deployment specs compared
  field by field against Prefect (parameters, schedule, work pool, image, job
  variables); `plan` reports creates/updates/deletes (hand edits show as drift),
  `apply` upserts each connection's deployment of `sync_connection` with a
  concurrency limit of 1 and workspace tags; `submit` runs the deployment with
  the operation ID as idempotency key and binds the flow run; `observe` closes
  unverified attempts from the flow run's state.
- Connections get an optional `schedule` (`{"cron", "timezone"}`), validated with
  `croniter` and `zoneinfo`; not part of the execution revision. New ingestion
  outcomes `lost` and `cancelled`, recorded by `refresh` and kept apart from
  `failed`.
- Metadata: `unverified()` (running attempts and submitted operations with no
  attempt) and `close_unverified()`.
- Service: `deployments_plan`, `deployments_apply`, `refresh`; the orchestrator,
  when configured, is also the executor. CLI: `deployments plan|apply` and
  `refresh` (refuse with exit 2 when `WORKSPACE_PREFECT_POOL` is unset);
  `serve` submits to Prefect instead of subprocesses when configured. Environment:
  `WORKSPACE_PREFECT_POOL`, `WORKSPACE_PREFECT_CONFIG_PATH`, `WORKSPACE_IMAGE`,
  `WORKSPACE_PREFECT_JOB_VARIABLES`. API: `POST /api/refresh`,
  `GET /api/deployments/plan`.
- `croniter` and `prefect-kubernetes` declared; the lockfile grew with the
  Kubernetes client libraries (no existing package changed version).

Validation:

- Lint, format, and mypy clean on the workspace slice. 70 tests pass on
  Python 3.14.7, including three against a real temporary Prefect API
  (`prefect_test_harness`, no worker): plan/apply converge, schedule drift and
  hand-edited parameters are updates, a removed connection is a delete; submit is
  idempotent on the operation ID; observe closes crashed as `lost`, cancelled as
  `cancelled`, leaves pending/running alone, and is stable on re-runs; invalid
  cron and unknown timezones are rejected. Two CLI tests cover the unset-pool
  refusal and that the credential commands still work without the orchestrator.

Next: the decision-012 runtime on the cluster — CI image build from `uv.lock`,
the Kubernetes work pool and worker controller in home-ops, the ExternalSecret
for the installation secrets, and the NFS artifacts mount; then cutover.


## 2026-10-28 — Workspace runtime: image, Kubernetes worker, secrets (decision 012)

Implemented:

- `Dockerfile`: one image for the runtime side (flow runs, the Kubernetes
  worker, later the workspace server) — Python 3.12, `uv sync --locked --no-dev`
  in a build stage, tzdata for schedule timezones, `PIPELINES_CODE_REVISION` set
  at build time, config path defaulting to the in-Job NFS location.
  `.github/workflows/image.yml` builds `ghcr.io/h-wb/pipelines` on push to main
  (branch, semver, and `sha-<long>` tags), following the sento image workflow.
- home-ops (`automation/prefect`, branch `feat/prefect-workspace-worker`): a
  `workspace-worker` controller on the pipelines image running
  `prefect worker start --pool workspace-k8s --type kubernetes`, with an init
  container that creates the pool (or updates its base job template) on every
  start; a namespaced Role for Jobs, pods, and logs; and the `pipelines`
  ExternalSecret (Bitwarden Secrets Manager → `pipelines-secret`).
- The pool's base job template (`workspace-job-template.yaml`): the default
  prefect-kubernetes template extended with the NFS export mounted at
  `/workspace/data` (fixed path so artifact storage paths resolve everywhere)
  and the flow-run environment. The warehouse credentials are injected straight
  from the CNPG-generated `postgres-metabase-app` secret (as for the other apps
  on postgres-metabase, nothing duplicated in Bitwarden); the workspace
  installation secrets (metadata URL, master key) come from `pipelines-secret`,
  placed by hand until the Bitwarden `pipelines` ExternalSecret owns it; the
  GitHub author-emails list is a placeholder for now. All via `secretKeyRef`
  (the Kubernetes worker preserves `valueFrom` entries and merges them with
  per-deployment env), so deployments never carry secret values.
- mise tasks `workspace:deploy` (deployments apply) and `workspace:refresh`,
  with the reconciliation environment (pool, cluster config path, image, job
  variables) and fnox grants for `WORKSPACE_METADATA_URL`.

Validation:

- `uv sync --locked --no-dev --python 3.12` installs and imports cleanly
  (prefect 3.9.0 matching the server, prefect-kubernetes, dbt, dlt, croniter).
- The custom base job template is accepted by a real Prefect API on pool
  creation, and renders Jobs with the NFS volume/mount, the secret env entries,
  and the deployment's job variables (namespace, image).
- home-ops: `kustomize build` clean, `helm template` against app-template 5.3.0
  clean, repo lint clean on the touched files (a pre-existing `hk.pkl` failure
  is unchanged).

Not yet verified end to end: `pipelines-secret` (metadata URL + workspace key)
is placed by hand in the cluster, the ProtonPass `env-dlt/workspace/*` secrets
must be created for the repo tasks, and the first `workspace:deploy` against
the real server is the remaining cutover step. (The image built in CI on the
first merge — a missing README in the build context briefly broke it — and the
worker plus pool are live on the cluster.)

## 2026-10-29 — The metadata URL composes from CNPG-managed parts

The metadata database is the same Postgres instance as the warehouse, so its
password was being copied a second time into whatever held
`WORKSPACE_METADATA_URL`. Instead, `metadata_url()` (in `src/core/config.py`,
imported by both the CLI and the flow — deliberately not `operations`, which
would drag SQLAlchemy into the read-only CLI) composes the URL from
`WORKSPACE_METADATA__{HOST,PORT,DATABASE,USERNAME,PASSWORD}` when
`WORKSPACE_METADATA_URL` is unset; a direct URL still wins.

Consequences:

- The workspace-k8s job template injects the metadata credentials from the
  CNPG-generated `postgres-metabase-app` secret, exactly like the warehouse
  credentials — no database password in Bitwarden, in the manual secret, or in
  Prefect. `pipelines-secret` (and the Bitwarden `pipelines` item, later) now
  holds one value: the workspace master key.
- The repo's mise tasks take `WORKSPACE_METADATA__PASSWORD` from the same
  ProtonPass item as the warehouse destination; the other parts are non-secret
  defaults in `mise.toml`. No new ProtonPass item for the metadata URL.

Validation: three new tests (direct URL wins, composition with quoting and the
CNPG-convention defaults, unconfigured is None); lint, format, and mypy clean on
the workspace slice; 73 tests pass on Python 3.14.7. The job template renders
Jobs with all fifteen environment entries resolved from the two Secrets.
