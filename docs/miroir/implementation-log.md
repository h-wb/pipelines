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

## 2026-10-29 — Connector migration: GitHub, Bike Share, browser history (decision 020)

Started by Codex (design, decision 020, connector packages, new sources) and
finished in a follow-up session after Codex hit its usage limit mid-test-update.

### What changed

- **Connector packages.** `src/integrations/<name>/` (listenbrainz, apple_health,
  github, bikeshare, browser_history) each own settings, credentials, check,
  resource declarations (`record_kind`: event/entity/snapshot; history retained),
  delivery metadata, an optional `FileInput` (validator, parser version, import
  options, artifact opener), and lazy `open_sync` hooks. `contracts.py` holds the
  dataclasses; `registry.py` only lists definitions; `formats.py` derives
  validators/parser versions from them. No integration-name or settings-type
  branches remain in `sources.py`/`runner.py`.
- **Sources are context managers.** `open_source`/`open_artifact_source` yield a
  dlt source and close sessions/temporary SQLite copies after success or failure;
  the runner runs the pipeline inside that scope. A connection's `resources`
  selection is applied with `with_resources`.
- **New sources, legacy untouched.** The new GitHub (API only; the token's account
  must match the configured login; no export seeding), Bike Share (merge,
  repeated-page detection, explicit `verify_tls`), and browser (read-only,
  size-bounded Firefox Places snapshot, plain or gzip; artifact imports read all
  rows) sources live in their packages. `src/sources/*` and the old flows are
  unchanged and remain the legacy path.
- **Analysis catalog.** `src/analyses/catalog.py` owns selectors, relations,
  versions, and typed parameters (`AnalysisDefinition.bind` maps validated
  parameters to dbt variables). Integrations name an analysis. GitHub identity is
  `github_login` (from the connection) and `github_author_emails` (analysis
  parameter, normalized), no longer a process environment override. Config
  validates `analysis.parameters` against the analysis and `resources` against
  the declarations.
- **dbt.** Every raw input of the GitHub, Bike Share, and browser staging models
  (and the two GitHub reporting models that read raw tables) now goes through
  `optional_input`: absent tables are empty and absent optional columns are null,
  so a new connection (no `export_*`, no `moz_places_metadata`, unselected
  resources) builds. Legacy builds still compile to `select *`. Source schemas
  take `miroir_sources` bindings. `github_my_emails()` prefers the analysis
  variable and falls back to `GITHUB__AUTHOR_EMAILS` for legacy builds, escaping
  quotes. `is_mine` on commits is now false (not null) when no identity matches.
- **Delivery roles and names.** `FileDelivery.roles` is enforced in
  `submit_artifact` (browser snapshots are `ongoing` only); the upload form shows
  only declared roles; automatic delivery names artifacts with the connector's
  `automatic_filename`.

### Validation

- Lint, format, and mypy clean on the slice (now including `src/analyses`).
- 110 tests pass (`MIROIR_TEST_POSTGRES_BIN` set; without it the dbt class errors
  in `setUpClass`). New: `tests/test_connectors.py` (catalog coverage, resource
  selection validation and application, analysis parameter validation and
  normalization, GitHub account mismatch check, Places snapshot validation, and
  browser role enforcement). Also in `test_analysis.py` on real Postgres: all
  three new analyses build from absent inputs; GitHub identity comes from the
  connection and analysis parameters; a browser snapshot import without
  interaction tables builds visits/domains.
- Read-only `information_schema` query of the warehouse's legacy raw schemas
  confirmed the column types declared in the staging models.

### Not done

- No release or deploy; no connections created on the cluster. Production cutover
  stays separate per decision 020 (inventory, drain writers, compare history,
  then switch schedules). Legacy GitHub export seeding remains a legacy operation.
- Web resource selection UI and analysis-parameter forms are not built; both are
  configuration-only for now.
- Cross-source packages (crossovers, `int_rides`) remain on the legacy path.

### Live release

Released `ghcr.io/h-wb/pipelines:sha-d040ebd0d2f625726228c618b2954b3645dbdded`
(image workflow 40946203307). The live configuration's `deployment.image` was
updated and applied through the interface; both Prefect deployments report the new
image. Home-ops commit `f2055b791` pins server and miroir worker; Flux reconciled,
both rollouts ready, server started cleanly with the live configuration. A
model-only operation (`38ed6214-6bbe-47a2-8ef7-78f20e00a9d5`) completed in Job
`hairy-alligator-76phw` on the release image. No metadata migration. No new
connections were created; legacy GitHub/Bike Share/browser deployments are unchanged.

## 2026-10-29 — Connector inputs and core-owned file delivery (decision 021)

Before cutting over GitHub, Bike Share, and browser history, settled how connectors
work across API pulls, manual imports, automatic delivery, and folders.

### Inventory (read-only)

- Legacy `local-pool` loads run nightly and succeed: `load_bikeshare` 00:00,
  `load_github` 01:00, `load_browser_history` 02:00, plus `run_dbt` 03:00.
- Raw datasets: `github_data` (1,229 API commits, 14,407 export commits, 4,014
  export PRs, …; dlt state `github` v33), `bikeshare_data` (342 trips, no duplicate
  rental IDs; state v33), `browser_history_data` (250,872 visits, 121,304 places;
  state v23). The new sources use the same dlt source names, resources, and state
  keys, so each connection can bind the legacy pipeline/dataset and keep history,
  `export_*` tables, and cursors.
- Miroir Jobs mount `Documents/data` at `/miroir/data`, so the browser snapshot is
  at `/miroir/data/browser/zen-places.sqlite.gz`. The Bike Share API's certificate
  now verifies; `verify_tls` stays on.

### What changed

- **Contracts.** `IntegrationDefinition` declares `fetch` (label, description,
  opener) and `formats` (`FileFormat`: ID, version, validator, typed `options`
  model, `interpretation` from settings, default folder pattern, sender metadata,
  parser). Capabilities and setup methods are derived; `input_modes`,
  capability strings, `delivery_methods`, `file_delivery`, `file_input`, and
  `open_sync` are gone, as is the global format table (`formats.py`): formats
  resolve through the connection's integration. `describe()` feeds the CLI, API,
  and forms.
- **Connectors.** Health and browser are format-only; their folder readers,
  Health's `manual/` convention, and browser incremental reads are removed (a
  snapshot is read in full and merged however it arrives). Health's per-file
  choice is `HealthImportOptions.role`. `data_dir` left the source settings.
- **Watched folders.** `folder: {path, pattern, format, options, keep_last}` on
  the connection, validated against the integration and its format's options; a
  legacy `settings.data_dir` is read as one. A sync fetches (under the lock), then
  collects the folder through `submit_artifact` (origin `folder`, retention
  `rolling`), imports each pending artifact as a child import operation
  (`parent_operation_id`), prunes older imported copies beyond `keep_last`, and
  builds once. Nothing new → ingestion `not_run`, no build. Invalid files are
  reported on the sync and never stored; valid ones still load and build.
  Retention excludes `keep_last` from the revision. Folder readiness is a core
  check. Flow runs find artifact storage at `MIROIR_ARTIFACT_DIR` or `artifacts`
  beside the configuration (the cluster's layout).
- **Import options.** Metadata schema 5: artifacts store `options` (JSON),
  `origin`, `removed_at`; `role` is migrated into `{"role": …}` in the same JSON
  form imports record, so existing receipts still match. Operations gain
  `parent_operation_id`. Upload forms render the format's options; the upload
  API treats non-reserved query parameters as options (`?role=historical` keeps
  working); the receiving URL always uses defaults; the CLI takes
  `--option NAME=VALUE` and `--format`.

### Validation

- Lint, format, mypy clean on the slice; 119 tests pass (Postgres bin set). New:
  `tests/test_folders.py` (child imports and a single build, unchanged folder
  loads/builds nothing, invalid files reported while valid ones load, retention
  removal, replay refusal and restoration on resend, missing folder, legacy
  `data_dir`, folder validation, retention outside the revision), a v4→v5
  upgrade test (role becomes options; the earlier import still counts), and a
  web test for folder settings and option fields.

## 2026-10-30 — Configured analyses and explicit cross-source inputs (decision 022)

Continued Claude's unfinished milestone-6 first slice after its session limit.
The connector cutover remains separate; this session changes code and documentation,
not live connections, schedules, reporting tables, or Metabase assets.

### What changed

- The independent catalog now defines packages with named, typed input roles,
  selectors, outputs, parameters, and versions. Top-level `analyses` configure
  instances, explicit connection links, timezone, parameters, and dashboard links.
  Each new connection gets its recommended default analysis. Legacy configurations
  without `analyses` upgrade on read, preserving source revisions, output namespaces,
  analysis settings, and dashboard links; explicit empty lists stay empty. Service
  edits persist the new shape. Invalid legacy analysis values still fail safely.
- Crossovers requires Health and optionally links music and biking. Its build uses
  actual raw bindings, selected accounts, and its own namespace. Unlinked sources
  are explicitly excluded from table discovery, even if a table exists at the
  placeholder schema. Daily/yearly measures and late-listening categories stay null
  for unlinked inputs. Legacy dbt invocations retain their behavior.
- Metadata schema 6 adds `operations.analysis_id` and build-outcome update times.
  Earlier build operations are backfilled under their connection IDs, preserving
  successful receipts. Each linked analysis builds once per load as its own child
  operation; one model failure neither fails ingestion nor stops the other builds.
  Folder syncs still build once after collecting/importing files; unchanged folders
  do not build. Explicit rebuilds pin the plan and never resolve source credentials.
- Analysis freshness checks all linked inputs, the current plan receipt, the latest
  completed outcome, and active builds. Loads arriving during a build leave it stale.
  Outcomes follow completion time, since builds submitted through different inputs
  can acquire the dbt concurrency slot in a different order. Migrated combined
  load/build records use ingestion time as the safe input boundary. Inline child
  builds/imports are observed through the parent's backend run while keeping that
  submission ID unique. Connection status continues to describe ingestion attempts.
- Added overview/detail/create/edit/remove UI and JSON API for analyses, explicit
  compatible input selectors, timezone/GitHub-parameter editing, per-input load
  evidence, model outputs, dashboard links, and build history with Prefect links.
  Connection removal refuses other analysis dependencies; its own default analysis
  goes with it. CLI `analyses` lists status; `models` accepts an analysis ID.
- Updated the README and roadmap with configuration/API/CLI examples, migration
  behavior, rollout requirements, and the remaining milestone boundaries.

### Validation

- Ruff lint/format and mypy passed across the maintained slice. Example configuration
  validation and `git diff --check` passed.
- The full unittest suite passed 130 tests, including the four real temporary
  Prefect API scenarios and seven disposable Postgres/dbt scenarios. Command:
  `MIROIR_TEST_POSTGRES_BIN=/home/paseo/.local/share/mise/installs/postgres/17.11/bin uv run --locked python -m unittest discover -s tests`.
- New tests cover legacy config and metadata migration, stable namespaces/receipts,
  invalid links/parameters/timezones, explicit empty analyses, UI/API conflict and
  removal behavior, CLI listing, one build per linked analysis, failure isolation,
  crash observation, freshness across inputs, a load during a build, and builds
  completing out of request order. Real Postgres builds cover Health-only crossovers
  with unknown optional measures, an occupied unlinked placeholder that is never
  read, and linked music/biking data in the same isolated output namespace.

### Release boundary and next work

This slice requires server, worker, and flow-run images on the same schema-6 runtime.
Drain active Miroir runs, back up configuration/metadata, then coordinate the image
pins and deployment apply. Old runtimes cannot read schema 6; rollback requires the
matching pre-upgrade backup. No live migration or connector cutover was performed
in this session. Milestone 6 remains open: managed Metabase asset ownership and
conflicts, a default installation bundle, and an exercised fresh install/restore
are subsequent slices.

## 2026-10-31 — Schema-6 rollout preparation

Prepared release of `sha-43f2838ea816b0997ef979d1e248bf8ae72e4688` (image
workflow 40991623452 succeeded). Live server, worker, and both Prefect deployments
still use schema-5 image `sha-190a3fd8457ed71c65d9f7e8b984965eed48cdb5`.
The server and worker are ready. Authenticated Prefect inspection found no active
Miroir runs and no schedules on the two Miroir deployments; existing connector
cutovers remain separate. The latest CNPG metadata backup completed
2026-10-31 00:00:06 UTC, and its ContinuousArchiving condition is healthy.

Home-ops local commit `f0ebc4109` pins the new image for the server and worker
(including its pool initializer). Both Kustomize builds and app-template 5.3.0
Helm renders passed; kubeconform validated eight resources and skipped three
custom resources, with no errors. Home-ops commit checks passed. That commit is
not pushed yet: the live configuration and deployment image must be updated first.

Access limitation: the pod's Kubernetes service account cannot read the Miroir or
metadata credential Secrets. Renewing the configured ProtonPass session restored
Prefect API access, but `env-dlt` has no `miroir` item, so its metadata-password
reference cannot resolve; no Miroir administrator API token is configured here.
There is also no connected authenticated browser. No live configuration or
metadata was changed. Requested the operator to save a configuration backup, set
`deployment.image` to the new SHA in `/config`, save, and Apply deployments.
After that step, push the prepared home-ops pin, reconcile both apps, and verify
readiness, a flow run on the release image, analysis outcomes, and empty drift.

### Live rollout completed

Recovered authenticated operator access through the documented Bitwarden CLI token
reference in home-ops `fnox.toml`, using the already-authorized ProtonPass session
(which can read `env-home-ops` as well as `env-dlt`). The Bitwarden `miroir` item
contains the application API token. Credentials stayed in process memory; Kubernetes
RBAC was not changed. The operator UI step requested above was not needed.

- Saved the original configuration, overview, deployment definitions, and pool/queue
  settings under `~/.local/state/miroir/releases/20261031T170841Z/`, with private
  directory/file permissions. Database recovery is covered by CNPG backup
  `20261031T000003` and healthy continuous WAL archiving. No new logical database
  dump was taken; the application metadata credential remains unavailable here.
- Paused only the Miroir pool and its default queue during maintenance. The queue
  guard also covers the worker initializer updating the pool. Applied the two
  reviewed image-only Prefect deployment updates through Miroir's authenticated API.
  Pushed home-ops `f0ebc4109813c738b5e65f8365d67aac3bac5221` and reconciled Flux
  source plus both applications. Miroir Helm release 14 and Prefect release 57 are
  ready; server, worker, initializer, and new flow Jobs use
  `sha-43f2838ea816b0997ef979d1e248bf8ae72e4688`.
- The schema-6 runtime started cleanly. Both existing analyses retained their
  successful build receipts/current status immediately after migration. Output
  relation identities and dashboard links match the saved pre-upgrade overview.
- Restored the original unpaused pool and queue settings. Submitted two model-only
  verification operations through the new analysis API. ListenBrainz operation
  `aae2049f-e518-4795-85c7-fc1e83852d65` completed in Prefect run
  `0187a8db-f291-4c7e-b31a-1cf13dabcb24`, Job `able-bulldog-fb2ws`; Health operation
  `4d11493b-4012-41d4-8c1a-c933f4dd7e96` completed in run
  `20f72e5f-13d8-48be-ba75-3192e81b8f9b`, Job `artificial-seal-kmdrv`. Both Jobs used
  the release image and recorded ingestion `not_run`, models `succeeded`, analysis
  `current`. Last successful ingestion timestamps stayed unchanged.
- TLS-verified requests through Envoy returned 200 for health, overview, Health
  connection, both analysis pages, and crossover creation. This pod lacks the LAN
  split-DNS record, so the verification process resolved the application hostname
  to the Gateway's advertised address while retaining certificate/hostname checks.
  Deployment drift is empty. Verification evidence is saved beside the config backup.

Schema 6 is now deployed. Connector cutovers, new cross-source configuration, managed
Metabase assets, and installation/restore validation remain separate work. No source
bindings, legacy schedules, or dashboard definitions were changed during rollout.


## 2026-11-02 — Share managed Proton Pass login with dotfiles

The repository's login previously reused any cached Proton session before checking
its supplied token. Replace that implementation with an extension of dotfiles'
global `proton-login` mise task template, retaining the existing `dlt` session and
macOS Keychain service. Operator tasks that request fnox secrets now depend on
login, so rotation/removal is checked before resolving their task grants.

Validation: with the updated dotfiles as `MISE_CONFIG_DIR`, `mise tasks validate
--errors-only` passed all 20 tasks and `mise tasks deps miroir:deploy` showed the
login prerequisite. Shared dotfiles regressions exercise token rotation, expiry,
removal, rejected credentials, macOS Keychain selection, session isolation and
ordinary/monorepo task inheritance with fake credentials. No product code, live
credentials, deployments, data or token grants changed. Update dotfiles before
using these operator tasks.


## 2026-11-02 — Follow credentials moved to Automation

Point the ListenBrainz, Bike Share and GitHub source references at the moved
Automation items, retaining field names and per-task grants. GitHub uses the
`github-pipelines` item to distinguish its credential from workstation access.
Keep the `prefect`, `postgres-metabase` and `miroir` operator references in
`env-dlt`: their existing BWS/CNPG sources require a separate operator-access
migration before those copies can be removed. Update setup documentation to
stop directing new source credentials into the legacy vault.

Validation: TOML parsing and provider/reference consistency checks passed; mise
validated all 20 tasks with the updated dotfiles template. No live secret fetch,
pipeline execution, deployment or token-grant change was performed. Agent grants
must include the moved Automation items before secret-consuming tasks can run.

## 2026-11-02 — Move infrastructure operator copies to env-home-ops, retire env-dlt

The remaining `env-dlt` items (`prefect`, `postgres-metabase`, `miroir`) exist so a
Paseo worktree or the workstation can run the operator tasks against the existing
cluster (Prefect API, warehouse and miroir metadata databases, miroir master key) —
the same dev-mode principle as the Sento project config. They are infrastructure
credentials, not pipeline source tokens, so they move to the **`env-home-ops`**
vault, which the Paseo pod token already reads; the workstation `dlt` token needs
item-level grants on those three items. `fnox.toml` gains an `infra` provider
(vault `env-home-ops`) for these references, and the legacy `env-dlt` vault is
retired once the references update. Authoritative sources stay the cluster's:
BWS items `prefect`/`miroir` and CNPG secrets `postgres-metabase-app`/
`postgres-miroir-app`; a later migration can point fnox at BWS directly and drop
the Proton copies entirely.

Validation: TOML parsing and provider/reference consistency checks passed; mise
validated all tasks with the updated dotfiles template. No live secret fetch,
pipeline execution, deployment, item move or token-grant change was performed —
the Proton Pass moves and item-level grants are operator actions listed in PR #3.


## 2026-11-03 — Local and infrastructure development launchers

Implement decision 023 with `mise run dev` and `mise run dev:infra`, exposed as
Paseo `dev`/`infra` service scripts. Both forward assigned host/port arguments to
the checkout's CLI. Local mode uses its own SQLite, DuckDB, config, artifacts and
persistent generated credentials under `.miroir/dev/`; inherited installation
settings do not select an orchestrator or database. Infrastructure mode depends
on shared Proton login and requests only the metadata password, encryption key
and Prefect URL/auth. It requires existing installation config, PostgreSQL
settings, a configured deployment and shared artifacts before starting. Its own
UI token stays under `.miroir/dev-infra/`; credentials never enter argv or logs.

Keep the infrastructure capability distinct from flow-code development: requested
Prefect operations execute the configured deployment image. Document that the
draft general Paseo host's read-only `/data` export is different from Miroir's
installation data; the `/miroir/data` mount still needs provisioning. Correct
current docs/comments that assumed item moves or pod-token grants were already
verified. The Proton operator copies still duplicate their BWS/CNPG sources.

Validation: all 135 unittest tests passed, including five new launcher tests for
inherited-setting isolation, persistent private credentials, infrastructure
selection and missing-prerequisite failures. Ruff lint/format and mypy passed for
the new launcher. All 22 mise tasks validate; `dev:infra -> pass:login` is present,
and `mise run dev -- --help` forwards arguments without Proton authentication.
An actual server in a disposable directory returned healthy and authenticated
overview responses, created only local metadata and ignored synthetic inherited
infrastructure settings. The temporary server was stopped and removed. No live
Proton secret retrieval, infrastructure launch, vault move, mount change or
deployment was performed.


## 2026-11-03 — Complete Proton organization and verify references

The authorized vault migration is complete: machine/source credentials use
Automation; infrastructure/bootstrap items use Infrastructure; human logins
remain in Homelab. Update the infrastructure provider and item references without
changing fields or credential values. Prefect operator authentication differs
from its human login and remains a separate item. The original env-dlt vault has
no active items. The existing Infrastructure/postgres-metabase item is preserved.

Live validation resolved 44 existing references across dotfiles, its Sento map,
home-ops and pipelines through fnox and compared each with direct Pass resolution
without logging values. The two Miroir installation fields were already absent:
Infrastructure/miroir must be populated from the existing CNPG password and BWS
key before infrastructure mode works. They are documented as provisioning
prerequisites, not reported as verified. No new encryption key was generated.

The 15 moved/renamed items were compared with their original content. The full
WireGuard note required a verified copy plus recoverable trash of the original
because pass-cli could read its folder-encrypted item but could not move the key.
The full and split profiles have different DNS/routing and are both retained.
Routine runtime tokens remain viewer agents; the temporary regular administrative
PAT is not installed as a runtime credential.

Older local checkouts have reference-only fnox.local.toml compatibility maps until
the tracked changes are merged. The pipelines compatibility file explicitly
preserves the cluster-profile Prefect endpoint; both default lookups and the
cluster override were checked. Remove compatibility maps after updating tracked
configuration. The live Paseo token still needs approved viewer grants in the
new vaults from the account owner; regular scoped PATs cannot manage other tokens.

Checks: all four maps parse and match their provider vaults, all 22 mise tasks
validate, five development-launcher tests pass, and 22 dotfiles regressions pass
(two platform skips). These are reference/documentation changes after the prior
135-test application run; application code and deployed workloads did not change.


## 2026-11-03 — Scoped general reader references

Add a native `paseo-general` fnox profile for the eight provisioned source,
warehouse and Prefect fields. The repository's mise environment selects it only
when `PASEO_DATA_SCOPE=general`, preserving explicit profile selection and the
workstation's default named-vault references. This is an authentication reference
change; source values, task grants, endpoints and runtime schemas do not change.
The two missing Miroir installation fields retain their documented prerequisite.

Validation: compare all eight fields from the new item-only agent through native
fnox with the prior named-vault reader, reporting equality without values. Mise
task definitions and the five development-launcher tests are also checked. No
live Miroir server, metadata migration or Prefect operation is started.


The rollout's native mise smoke check also found an older local `cluster`
profile selection on the provisioned host. Add `paseo-general-cluster`, inheriting
the item-scoped credentials followed by the existing cluster endpoint overlay.
Dotfiles selects that combined profile for its managed Paseo checkout; fresh
worktrees without the host override still use `paseo-general`. Verify source
credentials plus the unchanged in-cluster Prefect URL through real mise grants.


## 2026-11-11 — Repair the GitHub commits model; skip what is deleted mid-run; CI runs the suite

PR #8 deduplicated the `repos` input of `github_commits` but put `optional_input`,
which expands to a whole `select`, directly after `from`. Postgres rejects it
(`syntax error at or near "select"`) in both Miroir and legacy builds, so every
GitHub model build since that merge fails, including the legacy nightly
`load_github` refresh and `run_dbt`, which clone `main` at run time. Two of the
seven Postgres/dbt tests failed on `main`; CI only built the image.

- The model reads the input as a subquery. The GitHub Postgres scenario adds a
  stale and a recreated row for one repo: commits are not duplicated and the
  newest row decides `is_fork_repo`.
- The deleted-branch fix generalises: a repo, branch or commit that returns 404
  after being listed is skipped by every repo-level resource (commits, pull
  requests, issues, comments, releases, external commits), in the connector and
  in the legacy source the nightly load still runs. Other errors still fail the
  run. A reference test drives the real source through a fake transport (with
  the response hooks dlt raises from) for a deleted repo, branch and commit, and
  checks that a 403 still fails.
- `.github/workflows/test.yml` runs ruff, ruff format and mypy on the maintained
  slice and the full suite, with the runner's Postgres for the dbt scenarios, on
  pull requests and `main`.

Validation: all 136 tests pass locally (Postgres 17.11), ruff and mypy are clean
on the slice, and the legacy source was exercised with the same fake transport.
No image, deployment or live configuration changed. The `sha-da77141…` image
carries the broken model and must not be rolled out.


## 2026-11-11 — Audit fixes across core, transports and connectors (decision 024)

Following the GitHub repair above, a read-only audit of `src/core`, the web/CLI/
Prefect transports and the connectors/dbt found about thirty candidate defects.
Each fix below began as a test that failed on the previous code; candidates that
could not be reproduced, or that need a design decision, are listed at the end.

### Core

- Folder scans treated every file still in the folder as an arrival: each scan
  copied and fsynced it again, re-dated it and restored copies retention had
  removed, so `keep_last` kept files in name order and "last received" never went
  stale. A scan now hashes in place and reuses known content untouched; resending
  by upload or receiving URL still restores a removed copy (decision 024).
- Connection status showed a folder sync's last child import (no run link,
  "no transform configured") instead of the sync.
- An operation whose submission failed had no attempt and stayed requested
  forever; a refused rebuild left its analysis `running` and its page refreshing.
  Rebuilds prepare the deployment before recording the operation, and a refused
  submission closes the operation with a `blocked` `SubmitFailed` attempt.
- Unexpected check errors are recorded (redacted longest first) instead of
  escaping; a GitHub rate-limit 403 is not "credentials rejected"; metadata
  passwords with spaces survive the URL (the old test asserted the broken `+`);
  saving the configuration keeps its file mode; a damaged stored copy is replaced
  by the same verified bytes; Postgres migrations take an advisory lock; the
  connection-lock session runs in autocommit, and a lost session no longer turns
  a recorded outcome into a failure.

### Transports

- The setup page ignored that a linked build is the newest operation after an
  import, so first delivery never showed success.
- Settings forms are typed by the settings schema: Bike Share `verify_tls` could
  not be set to false and silently reverted to true on save.
- Prefect errors are product errors everywhere, including `DeploymentApplyError`
  on create and connection failures in submit/observe. Unreachable or refusing
  Prefect is `OrchestratorUnavailable` and answers 503, so Health Auto Export
  retries instead of treating an outage as a rejected file; resending is safe.
- The deployment plan compares the concurrency limit and every schedule, and
  apply removes slugged schedules Prefect would otherwise keep (verified against
  the temporary API: such a schedule was permanent drift).
- Malformed JSON or non-UTF-8 bodies are 400s; CLI `models`, `refresh` and
  `deployments` print errors instead of tracebacks; `serve` hands its subprocess
  executor an absolute SQLite path.
- File submission and connection checks run in the threadpool instead of on the
  event loop of the single server worker; a declared body over the upload limit
  is refused before a multipart form is spooled to disk.

### Connectors and models

- Bike Share stopped paging after two pages (it compared against dlt's moving
  `last_value`): a first run over 1,100 trips loaded 1,000, and a later run could
  leave trips behind its cursor for good. It now compares with `start_value`.
- ListenBrainz, GitHub and Bike Share fetches used a plain session: no retries on
  429/5xx and no timeout. They use dlt's retrying session (60 s timeout).
- A truncated or corrupt `.sqlite.gz` is an invalid file instead of aborting a
  folder sync; a ListenBrainz page that cannot advance fails instead of looping;
  checks fail readably on non-JSON answers.
- GitHub `is_mine`/`is_external_repo` compare logins case-insensitively.
- Browser view/typing minutes and key presses, crossover workout minutes and the
  yearly workout count are null when their raw table is absent and zero only when
  it exists without rows (decision 020). Legacy builds are unchanged.

### Review follow-ups

An independent review of the combined diff found two regressions it introduced,
both reproduced before fixing. A scan reused a removed copy even when its file
had to be read again (new cutoff or parser version), so every later sync failed
with "Retention removed the stored copy"; it now restores the copy from the
folder in that case. The setup page filtered a 20-row window, so a first scan of
20+ files hid the sync behind its children; the newest request is now queried
directly. Also: identical bytes arriving together (now possible within one
process, since submission left the event loop) re-read the winning row instead
of raising `IntegrityError`, and an intact stored copy is no longer replaced
under a reader, only a damaged one. Prefect 4xx answers stay "unavailable"
(503): they reflect the server's configuration, which a sender cannot fix but an
operator can, after which a resend succeeds.

### Validation

Ruff, ruff format and mypy are clean on the maintained slice. All 161 tests pass
locally with Postgres 17.11 (136 before this batch), including the temporary
Prefect API and Postgres/dbt scenarios; CI ran the suite on PRs #9 and #10. No
image, deployment, live configuration or data changed.

### Rollout notes and remaining work

No metadata schema change. Deployed after this, `deployments plan` may report a
deployment's concurrency limit or a hand-added schedule that it ignored before;
Apply clears it. Browser and crossover dashboards show blanks instead of zeros
where those tables were never delivered. Not changed here: GitHub windows by
commit/author date can miss branches pushed or PRs merged more than three days
late; browser rows are keyed by profile-local SQLite IDs; Health energy/distance
units are assumed rather than read from the export; `put_secret` can race into
a raw `IntegrityError` across processes.


## 2026-11-11 — Deployed sha-49bd0d3b (PRs #9 and #11)

Before: the server and worker ran `sha-da771418…` (home-ops `e3291d602`, pinned
this morning for PR #8, whose `github_commits` model Postgres rejects), while the
live configuration and both Prefect deployments still ran `sha-43f2838e…`. No
Miroir runs were active; Miroir has no GitHub connection, and no legacy run
started while `main` carried the broken model. No metadata schema change.

- Saved the configuration, overview, plan and analyses under
  `~/.local/state/miroir/releases/20261111T162743Z/` (private permissions).
- Set `deployment.image` to `sha-49bd0d3b8ac74e5a585dc99f74793c2792a2fca6`
  through the API and applied the two image-only deployment updates.
- Home-ops `436c98e85` pins the server and worker; Flux applied it (Helm
  releases miroir v16 and prefect v60 ready, both pods on the new image).
- The new server's plan, which now also compares concurrency limits and every
  schedule, is empty. Model-only rebuilds of both analyses ran in Jobs
  `aloof-bobcat-bvwqk` and `humorous-ladybug-pkv6g` on the new image: ingestion
  `not_run`, models `succeeded`, both analyses `current`.

Left as found: eight operations recorded before this release without any attempt
(a ListenBrainz sync from 2026-10-29 and seven older Health operations) still
count as requested and keep those pages refreshing; the release stops new ones
but does not close old ones. The legacy GitHub load and nightly dbt pick up the
repaired model from `main` at their next runs (01:00 and 03:00 UTC).


## 2026-11-11 — GitHub cut over to Miroir (decision 021)

The first legacy connector moved to Miroir with its history in place.

- Inventory: legacy `load-github` ran nightly at 01:00 UTC into pipeline
  `github` / dataset `github_data`; its last run failed (the deleted-branch 404
  fixed by #8), so the committed cursor was the 2026-11-10 run.
- Paused `load-github` in Prefect (its queued runs were dropped; nothing was
  running) before any Miroir run. Saved the configuration under
  `~/.local/state/miroir/releases/20261111T170103Z/`.
- Added connection `github` (user `h-wb`) with an explicit binding to
  `github`/`github_data`, the same 01:00 UTC schedule, and its analysis in
  `America/Toronto` with the author emails from the existing Pass item as a
  parameter. The token is in Miroir's encrypted store; the check verified the
  account. Applying created `sync-connection/personal-github` on
  `sha-49bd0d3b…`.
- First sync (flow run `tacky-tarantula`): ingestion and the GitHub analysis
  build (12 data tests, including uniqueness of commit URLs) succeeded in 6m17s,
  against 3–4.5 minutes for recent legacy nightlies over a shorter window. A
  run that lost the cursor would refetch every commit since account creation
  (hours, rate-limited), so the legacy dlt state carried over.
- `load_github` is removed from `prefect.yaml` so `mise run deploy` cannot
  recreate it; the deploy task no longer receives the GitHub token. The paused
  Prefect deployment stays as a rollback path. The legacy nightly `run_dbt` keeps
  building the `github.*` reporting tables from `github_data`, so existing
  dashboards keep updating; the analysis writes its own `miroir_github_…` schema.

Not checked: warehouse row counts. This agent does not read the Metabase
warehouse; compare `github_data` counts in Metabase if wanted. Rollback: pause
`personal-github`, resume `load-github`.


## 2026-11-11 — ListenBrainz cut over to Miroir (decision 021)

The existing Miroir connection `listenbrainz` loaded its own generated schema
`ws_8_personal_listenbrainz` (from a 2026-10-01 start date, no schedule) while
legacy `load-listenbrainz` filled `listenbrainz_data` nightly at 00:00 UTC. Both
sources use source `listenbrainz`, resource `listens`, key
`(listened_at, recording_msid)` and cursor `listened_at`, so a binding change
resumes the legacy cursor.

- Paused `load-listenbrainz` (last run completed 2026-11-11 00:00; its queued
  runs were dropped). Saved the configuration under
  `~/.local/state/miroir/releases/20261111T205618Z/`.
- Bound the connection to `listenbrainz`/`listenbrainz_data`, set its
  `start_date` to the legacy one (2019-06-11), and gave it the legacy schedule
  (00:00 UTC). The plan was a schedule-only update of `personal-listenbrainz`.
- First bound sync (flow run `beautiful-dragon`): ingestion and the music
  analysis build succeeded; the whole run took 9.3 s. With a 2019 start date, a
  lost cursor would have re-read six years at 1.5 s per 1,000-listen page, so the
  legacy state carried over. The analysis now reads the full legacy history.
- `load_listenbrainz` is removed from `prefect.yaml`, and the deploy task no
  longer receives the ListenBrainz token. The paused deployment stays as a
  rollback path; the legacy nightly `run_dbt` keeps building `music.*` from
  `listenbrainz_data`.

Not done: the old `ws_8_personal_listenbrainz` schema is left in the warehouse
(nothing reads it; drop it when convenient). Rollback: pause
`personal-listenbrainz`, resume `load-listenbrainz`.


## 2026-11-11 — Bike Share cut over to Miroir (decision 021)

Legacy `load-bikeshare` loaded `bikeshare_data` nightly at 00:00 UTC by
appending the newest page of rentals, with TLS verification disabled and no HTTP
status check (an expired token would have "completed" with no rows). Both
sources use source `bikeshare`, resource `trips`, cursor `sd` (initial 0) and key
`rental_id`, so the bound connection resumes the legacy cursor; the new one
merges on `rental_id`, so even a lost cursor would only re-read history.

- Paused `load-bikeshare` (last run completed 2026-11-11 00:00; queued runs
  dropped). Saved the configuration under
  `~/.local/state/miroir/releases/20261111T214958Z/`.
- Added connection `bikeshare` bound to `bikeshare`/`bikeshare_data`, on the
  legacy schedule (00:00 UTC), analysis in `America/Toronto`. Its member ID and
  token are in Miroir's encrypted store (from the existing Pass items). The
  check passed with certificate verification enabled, so `verify_tls` stays on.
- First sync (flow run for `personal-bikeshare`): ingestion and the biking
  analysis build succeeded.
- `load_bikeshare` is removed from `prefect.yaml`, and the deploy task no
  longer receives the Bike Share credentials. The paused deployment stays as a
  rollback path; the legacy nightly `run_dbt` keeps building `biking.*`.

Rollback: pause `personal-bikeshare`, resume `load-bikeshare`.


## 2026-11-11 — Health cut over to Miroir; stale pool removed

Health Auto Export already posted to the Miroir receiving URL (14 webhook
deliveries, the last at 04:54 UTC); legacy `load-apple-health` (weekly plus
n8n-triggered runs into `apple_health_data`) last ran on 2026-11-08. Miroir kept
its imports in a generated schema with its own cutoff (2026-10-01). Both sources
write `metrics`, `workouts`, `state_of_mind` and `medications` with the same
merge keys, and file imports carry no cursor.

- Paused `load-apple-health`. Saved the configuration under
  `~/.local/state/miroir/releases/20261111T221648Z/`.
- Bound the `health` connection to `apple_health`/`apple_health_data` and set
  its cutoff to the legacy 2026-06-04. The changed cutoff makes every stored
  export a new import, so the 21 retained exports were imported again, oldest
  first, into the legacy dataset: 21 flow runs completed, ingestion succeeded,
  and the Health analysis is `current` over the combined history.
- `load_apple_health` is removed from `prefect.yaml`; the paused deployment stays
  as a rollback path. The legacy nightly `run_dbt` keeps building `health.*`
  from `apple_health_data`, which receives the phone's exports again.
- Deleted the `workspace-k8s` Prefect work pool: no deployments, workers offline
  since 2026-10-29, nothing in home-ops recreates it.

Left for the operator: disable the n8n Health workflow; drop the unused
`ws_8_personal_health` schema. Rollback: unbind `health` (its generated schema is
untouched) and resume `load-apple-health`.


## 2026-11-11 — Refresh closes requests that never got a run

Eight operations recorded before the audit release had no flow run and no
attempt (a ListenBrainz sync from 2026-10-29 whose submission failed, and seven
older Health requests). The release stopped new ones, but `observe` only sees
operations with a backend run, so nothing closed these: they counted as
requested forever and kept the overview and connection pages auto-refreshing.

- `refresh` first closes requests older than an hour with no backend run (their
  own or their parent's) and no attempt, recording a `blocked` attempt
  (`NeverStarted`), then observes as before. Recent requests, queued flow runs
  and children of a run are untouched.
- `put_secret` retries once after a unique-key conflict, so a credential set
  concurrently ends as an update instead of a raw `IntegrityError` (the last
  open race from the audit).

Validation: ruff, ruff format and mypy clean on the slice; 163 tests pass,
including both new cases.

Deployed as `sha-7bf62e9a…` (home-ops `f86e060b7`); the plan stayed empty and
`refresh` closed exactly the eight requests. It also exposed a status
regression: each closure is a new attempt row, so connection status showed the
`NeverStarted` closure as the ListenBrainz and Health "last attempt". A request
that never ran is not something the connection did, so status now skips those
closures (they stay in the operation history).


## 2026-11-11 — Deployed sha-fcde1662 (PR #18)

Set `deployment.image` and applied the four image-only updates, pinned the server
and worker in home-ops `f0d37b9e1`, and reconciled. The plan is empty; all four
connections show their last real sync (all succeeded) and none is requested; a
Bike Share rebuild ran in Job `valiant-finch-dxtxg` on the new image and the
analysis is `current`. Configuration backup:
`~/.local/state/miroir/releases/20261111T231311Z/`.

Seen while checking for active runs: eight legacy `local-pool` flow runs have
been stuck in `Running` since 2025-10 and 2026-07 (six `load-listenbrainz`, one
`load-apple-health`, one `load-arc-timeline`). They are stale Prefect states, not
work; cancel them in Prefect when convenient.


## 2026-11-11 — Legacy loads retired (decision 025)

After the four cutovers, the legacy browser-history deployment was deleted at the
operator's request (its data stays in `browser_history_data`), and the n8n Health
workflow was archived by the operator. This change removes `src/sources/`, the
per-source flows in `src/pipelines/`, their `prefect.yaml` entries, `mise` run
tasks and environment, and rewrites the documentation around Miroir connections.
`transform.py` (dbt, the nightly `run_dbt`) and `miroir.py` stay. With the legacy
code gone, lint and mypy cover all of `src`, in CI as well.

Correction to the earlier entries and notes: the legacy worker mounts the NAS
export at `/root/.prefect/data`; its scratch volume is only underneath. The
browser flow was reading the Nextcloud-synced snapshot the Mac writes every 6h,
not an empty folder.

The operator's post-cutover check (Metabase, raw schemas): no duplicate keys in
`github_data.commits` (2,287), `listenbrainz_data.listens` (129,082),
`bikeshare_data.trips` (343) or `apple_health_data.metrics` (6.85M); the newest
rows are the ones Miroir loaded (listens to 18:49 UTC and a commit at 17:03 UTC
on 2026-11-11, Health to the phone's 04:54 UTC export; last ride 2026-11-01).
Then the four paused legacy deployments were deleted and eight stale `Running`
legacy flow runs (since 2025-10 and 2026-07) were cancelled. Prefect now holds the
four Miroir deployments, `run_dbt`, and the dawarich flows.

## 2026-11-12 — Resource-oriented integration authoring and selection (decision 026)

GitHub's source now only assembles eleven native dlt resource modules. Shared
requests, date-window helpers, and lazy account/repository discovery live in its
client module. Assembly makes no network calls; extraction still checks that the
token belongs to the configured account. Existing source/resource names, primary
keys, merge dispositions, output shapes, and `last_run` / `last_date` state keys
are preserved. Supporting repository requests do not require loading repos, and
owned-repository discovery is shared across selected resources in one run.

The connector resource catalog now describes selection/presentation, not a second
copy of dlt keys/cursors. Bundled integrations declare labels; GitHub includes
scope descriptions and history/cost warnings. Connection setup and settings render
generic resource checkboxes for fetched integrations. Resource-only edits use a
revision-checked service operation and `PUT /api/connections/{id}/resources`;
creation also accepts `resources`. Empty, duplicate, and unknown selections fail
before persistence. Null/omission retains the all-resources behavior. No file
selection, destructive data operation, plugin installer, or deployment is added.

Added `docs/miroir/integrations.md` with the contributor contract, native dlt
primitives, cleanup rules, selection semantics, and compatibility/testing guidance.
Tests load all eleven GitHub resources into temporary DuckDB using intercepted
HTTP; assert existing keys and merge hints, lazy assembly, selected-only requests
and tables, shared discovery, account mismatch, failure cursor rollback, and
cursor restoration after deselect/reselect into a fresh worker directory. Web/API
tests cover creation, defaults, validation, error redisplay, revision conflicts,
source revision changes, and preserving bindings/schedules/other configuration.

Validation: ruff check, ruff format check, mypy, and `git diff --check` pass.
The full suite passes: 170 tests in 167s, including the real Prefect API harness
and disposable Postgres/dbt scenarios using Postgres 17.11. The 25 focused
connector/GitHub/onboarding tests also pass after strengthening the fresh-worker
cursor restoration and network-free assembly assertions. No live configuration,
warehouse, or deployment was changed; browser visual verification was not run.

## 2026-11-12 — Integration and connection-edit cleanup (decision 027)

Added shared safe integration diagnostics and rate-limit classification. GitHub's
check and extraction share account validation, classify mismatched accounts as
credential rejection, and report malformed account JSON/dates readably. Exhausted
quota (403/429) no longer marks a valid token rejected; a successful last-quota
response still succeeds. The runner understands typed errors through dlt wrappers.

Settings/resources/folder mutations share a private revision-checked context
manager, resolving from the same snapshot that is edited. Exceptions never save;
the final atomic save still checks the file revision. Web edit errors share a
renderer and keep submitted values/revisions. Resource JSON shapes are checked at
the HTTP boundary, and service resource parameters are `list[str] | None`.

GitHub resource metadata uses named dataclass fields instead of positional tuple
rows. Repository discovery is a public cached property without a forwarding
method; the lazy `authenticated_client` property makes verification explicit.
Sessions and date-window interfaces now use concrete types. No custom dlt base
class, cursor migration, or endpoint framework was introduced.

Validation: ruff, formatting, mypy, `git diff --check`, and focused tests pass. The first
full run found an existing test expecting rate limits to have no classification;
it now expects `rate_limited` (still not rejected credentials). The full suite is
green: 174 tests in 169s, including the real Prefect API and disposable Postgres/dbt
scenarios. New tests cover limits, valid last-quota responses, malformed account
bodies/dates, wrapped/raw HTTP failure classification, and invalid resource shapes
before secret/configuration persistence. Shared edits are tested for exception
rollback and final-save conflict detection, preserving concurrent file changes.
An isolated compatibility probe loaded the original GitHub source from `HEAD`
into temporary DuckDB, then resumed it with the modular source: all eleven table
schemas/hints matched exactly and the original committed cursor was reused.

## 2026-11-12 — GitHub native REST resources and incremental lag (decision 028)

A follow-up that compared the connector with dlt's documented patterns (dlt 1.31.0)
and adopted four of them. `repos`, `stars` and `events` are now declarative
`rest_api_resources` definitions. They share header-link pagination, `per_page`
and merge defaults, and an auth wrapper that runs the same lazy account check
before the first request. An extraction-only `owned_repositories` parent
(`selected=False`, owner affiliation only) is piped into the `pull_requests`,
`issues`, `issue_comments` and `releases` transformers. dlt runs it for a
selected child without loading its table, and it stays separate from the broader
`repos` stream.

Issues, comments and pull requests track native `updated_at` cursors with a
three-day lag; the calendar tracks `date` with a three-day lag. On the first run,
`github/incremental.py` seeds each untouched native cursor from the legacy
`last_run` / `last_date` inside extraction. A failed run therefore rolls back the
seed. It keeps one start boundary per resource across transformer rebinding and
never advances the legacy keys again. Pull-request early stop is per repository.
Commits and the two search resources keep their run windows. The dlt HTTP client
gets a `retry_condition` for rate-limited 403 responses, on top of the default
429/5xx retries and Retry-After. Plain 403/401 responses are not retried, and
search pacing is unchanged.

Tests cover migration once then native resume, rollback of a failed migration,
an empty migrated resource keeping its watermark and lag, lagged refresh of
existing rows across repositories, calendar migration and refresh, per-repository
PR early stop, declarative pagination/headers/scope, and the retry predicate
matrix. Downgrading after native cursors advance would resume the older legacy
watermarks: merge re-reads, not data loss.

Validation: ruff check, ruff format check, mypy and `git diff --check` pass.
The full suite is green: 182 tests in 180s, including the real Prefect API harness
and disposable Postgres/dbt scenarios (Postgres 17.11). No live configuration,
warehouse or deployment was changed. The live GitHub connection will run the
legacy→native seed on its first run after the next image rollout.
