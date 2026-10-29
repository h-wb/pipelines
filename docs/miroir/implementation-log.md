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

## 2026-10-29 — Renamed to Miroir; the metadata store moves to its own Postgres

Naming (decision 014): a full rename, done before the first cutover while every
name was still cheap to change — Python identifiers, the `pipelines-miroir`
CLI, `miroir.json` and `.miroir/`, all `MIROIR_*` environment variables, the
`miroir_meta` schema, the `miroir` secrets-provider value, the web interface
title, the Prefect pool `miroir-k8s`, and the docs directory. Roughly 700
occurrences across code and tests.

Storage (decision 015): the metadata store leaves postgres-metabase for its own
CloudNativePG cluster `postgres-miroir` (home-ops `automation/miroir`, the
repo's standard cnpg/initdb component, Barman backups), so the BI database and
the pipeline control plane no longer share a lifecycle. Warehouse data stays on
postgres-metabase; only the `MIROIR_METADATA__*` parts re-point, taken from the
generated `postgres-miroir-app` secret by the job template.

Validation: 73 tests pass on Python 3.14.7 (`-W error::ResourceWarning`);
lint, format, and mypy clean on the renamed slice; no "workspace" remains in
code, configuration, or the living docs (older decisions and log entries keep
their original wording as history).

## 2026-10-29 — The server is deployed on the cluster

Decision 007's remaining milestone: the web interface/API runs in home-ops
(`automation/miroir`) on the same image as the flow runs — `pipelines-mirroir
serve` behind envoy-internal at `miroir.<domain>`, metadata in postgres-miroir,
artifacts and configuration on the NFS export at `/miroir/data/miroir/`. Its
environment mirrors the repo's `mise mirroir:deploy` exactly (pool, cluster
config path, image, job variables), so the server's and the operator's
plan/apply never fight over drift. Requests are submitted as miroir-k8s flow
runs; the local-subprocess fallback (`--duckdb`) is now optional and only
required without `MIROIR_PREFECT_POOL`.

The server waits for the `miroir-secret` Secret (MIROIR_SECRET_KEY and
MIROIR_API_TOKEN, placed by hand until the Bitwarden `miroir` item exists) —
the pod starts the moment it lands. Validation: helm template against
app-template 5.3.0 renders the expected deployment, service, and HTTPRoute;
repo lint clean; CLI lint/format/mypy clean and 73 tests still pass.

## 2026-10-29 — First end-to-end sync on the cluster

A ListenBrainz connection added through the interface (credential stored
encrypted in postgres-miroir, check passing), deployments applied from the
interface, and Sync now: a miroir-k8s flow run, its own Kubernetes Job (20s,
image sha-e9a908c), dlt load into the generated dataset in postgres-metabase,
dbt models rebuilt, attempt recorded. The whole decision-007 runtime is live.

Shipping the server surfaced four fixes: the ExternalSecret still fetched the
old Bitwarden item name (rename missed dataFrom.extract.key); commands invoke
the absolute venv interpreter instead of PATH-resolved console scripts; serve
bootstraps the config parent directory on a fresh NFS export; and the composed
metadata URL pins the psycopg2 dialect (current SQLAlchemy resolves bare
postgresql:// to psycopg 3, which is not installed). The server also now sends
the Prefect API auth string (it submits flow runs like the worker does), the
interface gained Apply deployments (the plan exposed drift but only the CLI
could apply), and the Prefect adapter raises OperationError with instructions
instead of raw HTTP exceptions — a missing deployment or a refused request is a
readable error page, not a 500.

## 2026-10-29 — Deployment settings moved into the configuration (decision 016)

The `deployment` section (work_pool, image, job_variables) is authoritative per
field; `MIROIR_PREFECT_POOL`/`MIROIR_IMAGE`/`MIROIR_PREFECT_JOB_VARIABLES` are
fallback defaults, and either source enables the Prefect adapter — so the
interface and the CLI can no longer disagree about desired execution state.
The section is revision-neutral, like schedules. The mise task dropped its
image/job-variable pins (the configuration carries them); the deployed server
keeps its environment values as fallbacks until the miroir.json on the NFS
gains the section. Four new tests cover precedence, fallback, the missing-pool
error, and revision neutrality; 77 tests pass, lint and mypy clean.

## 2026-10-29 — File-first connections and automatic delivery (decision 017)

Apple Health setup required a worker-visible folder even when the user only
wanted to upload a file or receive phone exports. The parser/import path already
accepted retained artifacts; the connection schema, check, and UI still exposed
the old scanner as mandatory.

Implemented:

- `data_dir` is optional and shown under an advanced folder-scanning disclosure.
  New connections and the example config need only the cutoff. A receiving-only
  check describes readiness, and Scan folder is shown/enabled only with a folder.
  Existing folder configurations still scan and check normally. Direct scan
  requests without a folder return a readable error before registering an operation.
- The connection page offers upload and automatic delivery together, with a
  copyable URL, Health Auto Export JSON/header instructions, first-export/last-receipt
  status, and token creation/replacement/revocation. Tokens are shown once on a
  non-cacheable page; subsequent pages/API summaries never reveal their values.
- `POST /api/connections/{id}/receive` authenticates a connection-scoped token
  before reading the body, retains raw JSON as ongoing data, and requests an
  import through the shared service/executor. The response exposes receipt and
  operation IDs, not internal paths. Receive-only tokens cannot read/administer
  Miroir, target other connections, choose historical imports, or request replay.
  Administrator-only API routes also create/revoke delivery tokens. Removing a
  connection revokes its token. No master key is required for file reception.
- Metadata schema 4 adds a delivery-token table (random tokens stored only as
  SHA-256 hashes) and `artifacts.last_received_at`, initialized from the original
  receipt on upgrade. Duplicate exports advance the latest receipt without
  changing first receipt, artifact identity, or successful-import deduplication.
  Webhook imports record their origin as `webhook` in history.

Validation:

- Ruff check/format and mypy clean on the Miroir slice. All 84 unittest tests pass
  (72 seconds), including the real ephemeral Prefect API tests. Seven added tests
  cover folder-free creation/browser import, scoped delivery/import/deduplication,
  rotation/revocation/removal/restart, invalid/oversized/unauthenticated bodies,
  one-time token rendering and cross-origin refusal, queued imports/legacy scanning,
  and upgrading an existing v3 database while preserving history and receipts.
- A disposable local PostgreSQL 17.11 instance verifies the actual schema-qualified
  v3 upgrade, repeat migration, native token upsert/revocation, and duplicate receipt
  timestamps. No cluster database was used.
- Browser check against a temporary server: viewed folder-free connection setup,
  generated a token, sent a synthetic Health Auto Export JSON payload through the
  receive endpoint into DuckDB, repeated it (already imported), and confirmed the
  same token receives 401 from the admin API. The page shows the receipt, successful
  import, and webhook history. Synthetic preview uses temporary state only.

Not deployed in this session. Release the server, worker, and flow-run image
together because older runtimes refuse metadata schema 4. Real phone reachability
and representative exports remain unverified; the cluster route is internal, so
the phone needs the home network or VPN. Apply the connection's deployment before
its first production import, verify receipts/imports, then perform decision 009's
explicit n8n/legacy cutover. No live schedules, credentials, bindings, or n8n
workflows were changed.

## 2026-10-29 — Deployed file delivery and config-owned execution settings

Released pipelines `78138ad82abf7a64ae2718e58de21f9c6c016e99` through the
successful Image workflow; home-ops `26f1fcfc4d8468cae6ae02969b93dc48bf66a670`
pins that SHA image for the server and Kubernetes worker. Both HelmReleases are
ready after Flux reconciliation (miroir v9, prefect v51), and the new server
started successfully with metadata schema 4.

The live configuration now carries `deployment` with pool `miroir-k8s`, the
release image, and namespace `automation` (decision 016). Saved through the API's
revision check, then applied: the sole existing deployment's image was the only
change, and the subsequent plan is empty. Removed the three environment fallback
pins from the server HelmRelease. No schedules or existing dataset bindings changed.
Private pre-release configuration, overview, and deployment-plan snapshots were
kept outside Git. CNPG reported healthy replication/archiving and a successful
backup before release; there were no Miroir runs in flight.

Validation:

- home-ops pre-commit formatting/safety checks, both Kustomize builds, and Helm
  rendering against app-template 5.3.0 passed. Pushed main and reconciled the
  Flux GitRepository plus the prefect and miroir Kustomizations.
- A temporary file-only connection created through the live API passed its check
  without a folder. Created a scoped delivery token; it was refused access to
  configuration, accepted authentication at the receive endpoint, rejected invalid
  export JSON without creating artifacts/operations, and stopped authenticating
  after revocation. Removed the temporary connection; deployment drift is still
  empty. No synthetic warehouse data was loaded.
- A manual incremental ListenBrainz sync completed successfully in Prefect on a
  Kubernetes Job using the release SHA image, with the attempt recorded in Miroir.
  No new load IDs were produced on this repeat sync. Transformation was skipped
  because the generated dataset is not `listenbrainz_data`; analysis correctly
  remains `not_modelled`.
- HTTPS `/healthz` returned 200 through envoy-internal with TLS verification. The
  pod's DNS does not resolve the internal hostname, so this route check explicitly
  resolved it to the gateway's address while preserving Host/SNI. Both workload
  images and the connection's Prefect deployment match the release image.

The deployment is complete. Real Health Auto Export delivery still requires a
phone on the home network/VPN. Create the desired Health connection and delivery
token, apply its deployment, and verify a real JSON export before the explicit
n8n/legacy cutover. Existing n8n workflows and legacy schedules remain active.
An application-only rollback to schema 3 is unsupported; retain schema-4-capable
runtimes or restore the pre-migration database with a coordinated rollback.

## 2026-10-29 — Guided connection setup and separate settings (decision 018)

The webhook existed, but users had to add a connection, discover token management
on its activity page, and apply deployments separately. Adding a connection now
leads through delivery selection, the relevant settings/credentials, and a first-data
step. Apple Health offers Upload files and Health Auto Export, with server folders
under advanced methods. ListenBrainz asks for source credentials and then offers
the first sync. Existing connections can revisit setup from Settings.

Implemented:

- `src/integrations/delivery.py` describes delivery methods and file presentation;
  integrations declare these in the registry. Forms separate method-specific
  settings from account/parser settings. Upload accept types, sender instructions,
  and role explanations come from metadata. CLI/API integration metadata exposes
  methods too. Sync readiness uses the same declarations as onboarding.
- `src/web/connections.py` owns setup/settings routes, with shared form components.
  Activity keeps status, uploads, files, and history. Settings holds non-secret
  configuration, source credentials, delivery methods/tokens, and removal. Settings
  writes are revision-checked patches that preserve other connections, bindings,
  schedules, dashboard links, credentials, and installation settings. Form snapshots
  hash the same bytes they read so concurrent edits cannot receive a fresh revision
  attached to stale field values.
- Enabling automatic delivery prepares execution before issuing a one-time token.
  Viewing/retrying setup never replaces an existing token. A waiting page follows
  automatic receipt and import independently of manual uploads; one-time secret
  pages are uncacheable and never auto-refresh. Failed preparation keeps the saved
  connection and lets setup retry without re-entering settings or credentials.
- `MiroirService.prepare_connection` scopes plan/apply to create/update changes for
  its connection. Plan entries carry the connection identity; unrelated drift and
  deletions are never applied. Sync/import requests also prepare their own execution,
  so first use no longer requires a global Apply deployments action. The Prefect
  adapter translates preparation HTTP/not-found failures into readable product errors.
- Administrator API additions: revision-checked settings patches and connection
  preparation. The selected onboarding method is navigation only: file uploads,
  receive tokens, and an optional folder can coexist. No config or metadata schema
  migration, binding change, or automatic token rotation is introduced.

Validation:

- Ruff check/format and mypy pass on the Miroir slice. All 91 unittest tests pass
  (76 seconds), including four against the real ephemeral Prefect API. Added tests
  cover delivery-specific fields, no writes for invalid methods/settings, complete
  webhook onboarding/import, idempotent token setup, no false sender verification
  from a manual upload, failed preparation/retry, scoped application against real
  deployment drift/deletions, settings conflicts/preservation, and the same UI for
  another file integration declared only through registry metadata.
- Browser preview against temporary state: selected Apple Health delivery, continued
  through creation and token setup, inspected the waiting page and Settings, and
  confirmed token responses have no-store headers and no automatic refresh. The
  activity page keeps uploads available alongside automatic delivery.

Deployed as pipelines `b1f8e8cd49203d9f058ea8c63efae5d1b0183428`, home-ops
`24fde9524934a04a5493e7da305b9f68f1563abd`. The image workflow, both Kustomize
builds, Helm renders, and home-ops pre-commit checks passed. Config-owned execution
settings and both existing deployments point to the release SHA; server/worker
HelmReleases are ready (miroir v10, prefect v52).

Live validation created a temporary file-only connection through the onboarding
form, enabled automatic delivery, and verified that only its own deployment was
prepared without a global Apply step. Retrying setup preserved the token; invalid
JSON was rejected and no synthetic files/data were imported. Removed the temporary
connection and explicitly deleted only its test deployment; final drift is empty.
A repeat ListenBrainz sync completed on the new Job image with its successful
attempt recorded. Existing Health settings and its delivery token were preserved.

The HTTPS gateway check found that Uvicorn's localhost-only proxy trust generated
HTTP receiving URLs. home-ops now sets `FORWARDED_ALLOW_IPS` to the ingress pod
network. Verified through the actual TLS gateway that the settings page advertises
an HTTPS receiving URL and browser login sets a Secure cookie. No public-origin
guessing or source-specific URL logic was added to the application.

Real phone delivery and the n8n/legacy cutover remain separate; no live connection
credentials, schedules, or dataset bindings were edited during this release.

## 2026-10-29 — Connection-bound analysis after verified phone delivery (decision 019)

The user verified real Health Auto Export delivery. The next slice connects those
raw datasets to source-local analysis without moving their bindings into legacy
schemas or requiring unrelated sources.

Implemented:

- `src/core/analysis.py` resolves registry model metadata into a versioned plan:
  actual source schema, stable identity-derived output namespace, timezone,
  source-local dbt selector, and reporting relations. Health/music select their
  models and ancestors rather than all source descendants. Multiple accounts
  have separate staging/reporting schemas; legacy commands keep their bindings,
  schemas, inferred view types, and existing crossover builds.
- dbt source schemas accept explicit per-run bindings; schema generation accepts
  an isolated Miroir namespace. Miroir staging handles absent resources/optional
  columns and numeric variant columns from evolving dlt schemas. Required fields
  on present tables fail explicitly; unavailable metrics remain null. Each dbt
  invocation uses a fresh parse and temporary target/log directories.
- Successful loads invoke the resolved plan. Model-only operations use the same
  executor and connection lock, resolve no source credentials, and leave ingestion
  timestamps/load IDs unchanged. Added the API/UI rebuild action and Prefect CLI
  `models` command. Attempts distinguish model running/failure/lost/cancellation
  from ingestion. Observation preserves committed ingestion and conditionally
  closes only still-active stages, protecting outcomes recorded during observation.
- Freshness checks the successful build receipt/configuration as well as load/build
  times. A changed timezone/configuration or plan version requires a rebuild.
  CLI and web use the same projection. Activity shows analysis first, relevant
  dashboard links, rebuild, and collapsed output-relation details. Analysis timezone
  settings are revision-checked and separate from source/delivery settings.
- Crossovers stay explicitly unavailable for Miroir until their health, music, and
  bike-share inputs can be linked. Existing legacy schedules/triggers and Metabase
  assets are preserved. Dashboard packaging/reconciliation is still later work.

Validation:

- Ruff lint/format and mypy passed for the whole maintained slice including
  `src/pipelines/transform.py`.
- The full unittest suite passed 100 tests, including the four real Prefect API
  scenarios and three disposable Postgres/dbt scenarios. The latter exercise
  automatic load/build with real Postgres metadata, two health accounts, explicit
  raw binding, metrics-only exports, new sleep/workout columns, numeric variants,
  sparse music, timezone-only rebuild without source access, absent resources,
  required-column failure, and preservation of a sentinel legacy table. Set
  `MIROIR_TEST_POSTGRES_BIN="$(mise where postgres@17.11)/bin"` to include them.
- Live read-only inspection confirmed health currently has a metrics table and no
  workouts table; its units match the existing domain models. Metabase's installed
  OpenAPI and a read-only native query were checked before preparing new analysis
  links. Release/runtime verification is recorded below after rollout.
- Browser inspection with synthetic data confirmed the analysis card is visible
  before uploads/files, shows current freshness and its dashboard/rebuild actions,
  and keeps relation names collapsed. Added logical SQL dashboard examples under
  `examples/analysis-dashboards.json`; they are seeds, not reconciled managed assets.
- Release review separated source revisions from analysis receipts: timezone edits
  preserve queued ingestion and existing source revision identities; queued
  rebuilds refuse changed analysis plans. A model-only operation does not claim
  changed source settings were applied. Failed/interrupted rebuilds mark analysis
  stale even without another load, since dbt may have partially replaced outputs.

### Live release and verification

Released `ghcr.io/h-wb/pipelines:sha-2d2981c7653014b3388cc0896260122a8de06558`
(image workflow 40936853762 succeeded). Updated the live configuration's
`deployment.image` and applied its two image-only deployment changes. Home-ops
commit `391d1c0156da24671804ddc4e5df9491d8123695` pins the same runtime for server
and worker. Both Kustomize builds, app-template 5.3.0 renders, and home-ops commit
checks passed. Reconciled Flux source + both applications; Miroir release 11 and
Prefect release 53 are ready. No metadata schema migration was required.

- Set the connections' analysis timezone to America/Toronto, matching the existing
  installation's dbt timezone. Source execution revisions/configuration-applied
  status stayed unchanged. New model-only requests completed in Prefect on Jobs
  using the release SHA: Health operation `0aa9c40d-533f-4209-8201-4581e3448224`
  and ListenBrainz operation `cf34b9de-4577-447b-8ce7-79aa4d0984a3`. Both recorded
  ingestion `not_run`, models `succeeded`, analysis `current`.
- Created new, user-owned Metabase assets in collection 12 from the logical SQL
  examples: Music dashboard 32 (six questions), Health dashboard 33 (eight
  questions). All fourteen native queries executed successfully, including the
  empty workouts table. Stored links on the appropriate connections and configured
  Metabase/Prefect base URLs. Existing dashboard definitions were not replaced;
  there is no automatic asset reconciliation or claim of dashboard portability.
- Replayed one retained, previously successful real phone export, operation
  `c89c5a12-a0b3-4baf-be69-1ed4c63ae9a2`. Its import and automatic health build both
  succeeded, and Prefect/Job completed on the release image. Raw health row count
  stayed 1,790; no synthetic health data was introduced. Compared daily steps
  against raw measurements and the fresh Metabase question result. The values
  agree, and both legacy Health/Music dashboards' queries/layouts are unchanged.
- TLS-verified `/healthz` and authenticated connection pages returned 200 through
  Envoy. Both pages show their correct new dashboard link and analysis controls.
  Deployment drift is empty; both connections report current analysis.

Remaining boundaries: cross-source input linking, managed dashboard ownership,
legacy/n8n schedule cutover, and other connectors are still separate work. There
are eight older unstarted operation records (seven Health, one ListenBrainz) with
no backend binding; Prefect had no active Miroir runs before release. They were
not silently executed/cancelled during this work. Submission recovery/cancellation
needs a separate treatment rather than treating absence of a backend ID as proof
that an ambiguous submission failed. Operator credentials used for release were
kept outside the repository and removed from temporary files after verification.
