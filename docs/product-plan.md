# Personal data workspace: implementation plan

Status: implementation started, 2026-10-28.
See the [implementation log](workspace/implementation-log.md) for actual milestone
status and the [decision log](workspace/decisions.md) for choices made during development.
Completion criteria below remain gates, not claims of completed work.

## Objective

Turn this working personal warehouse into a coherent, extensible application for
power users. Keep personal data in an owned database, preserve original inputs
where useful, support APIs and manual exports equally well, and connect ingestion,
models, and dashboards through one configuration and status interface.

The first beneficiary is this installation. External user testing, a hosted
service, and a large connector catalog are not prerequisites.

## Review findings and resulting changes

The architecture is viable, but the first draft delayed useful feedback and left
several data correctness and execution responsibilities implicit:

1. A registry-only first delivery proves little. Validate contracts through a real
   API load and a manual import early, then provide a small interface before migrating
   every connector or implementing portable dashboard packages.
2. Multi-account configuration is insufficient without isolated runtime credentials,
   dlt state, and source namespaces. Verify actual independent executions early.
3. Artifact replay cannot reuse the current Apple Health directory scanner unchanged:
   its modification-time cursor can skip files and its `/manual/` path controls which
   dates are included. Explicit artifact metadata must replace those assumptions.
4. Archive preservation conflicts with truncate-and-refetch refreshes when the source
   no longer retains history. Define rebuild, backfill, and upstream deletion policies
   before exposing those operations.
5. Scheduled Prefect runs and n8n triggers bypass CLI request submission. They still
   need run registration, immutable configuration references, and recovery.
6. Files as configuration need a writable deployment location and conflict handling
   before a frontend can edit them. A worker's checkout is not that location.
7. A completed dbt build does not prove completeness or a consistent cross-source
   snapshot. Dataset coverage and model input versions need explicit semantics.

These are design findings from the current code, not production failure reports.

## Decisions and boundaries

- Use Python, dlt, Prefect, Postgres, dbt, and Metabase for the initial implementation.
  Validate the execution boundary early; avoid creating generic backend abstractions
  until a concrete second implementation is needed.
- Build a small workspace core and CLI; the frontend later calls the same core.
- Treat integration definitions as trusted Python code plus declarative metadata.
  Start with an explicit bundled registry. Local third-party discovery and installation
  can follow once adding a bundled integration has established the extension contract.
- Support multiple configured accounts per integration in a single-person workspace.
  This does not introduce hosted multi-tenancy or household sharing.
- Use files as the authoritative desired configuration. Store observed state and
  execution references in a separate metadata schema in Postgres.
- Preserve existing production behavior through explicit compatibility mappings.
  Moving files into a new layout must not silently reset dlt state or rename tables.
- Start export-only integrations with manual acquisition. Browser automation,
  device clients, and AI analysis are later additions.
- Expose capabilities explicitly: incremental sync, export import, resource selection,
  date-bounded backfill, and rebuild are not universally available operations.

## Meltano evaluation and decision

Meltano 4.4.1 was evaluated in a disposable project using synthetic CSV data and a
custom dlt utility loading DuckDB. Installation, setting injection, named job
execution, repeat imports (2 rows stayed 2), and an overlapping export (3 rows)
worked. dlt performed the merging; Meltano provided configuration, isolated plugin
installation, invocation, and logs. No production connectors were migrated.

Meltano is a reasonable foundation for a Singer-based project. Keeping dlt as a
utility preserves its state ownership and leaves the product-specific work to us.
For this repo, adding Meltano now does not remove enough responsibility to justify
another configuration and execution layer. Do not introduce it as a required core
dependency. Reconsider if Singer connector reuse becomes a concrete need.

Relevant primary documentation:

- [Meltano plugin model and custom utilities](https://docs.meltano.com/concepts/plugins)
- [External orchestration](https://docs.meltano.com/guide/orchestration)
- [Removal of the old self-hosted UI](https://docs.meltano.com/guide/v3-migration)
- [Separate Meltano Cloud offering](https://docs.meltano.com/meltano-cloud/cloud-overview)
- [CSV imports](https://hub.meltano.com/extractors/tap-csv/)
- [Apple Health XML connector](https://hub.meltano.com/extractors/tap-applehealth/)
- [Metabase utility](https://hub.meltano.com/utilities/metabase/)

These are documentation findings plus a small local test, not a production
benchmark or a claim that existing Singer taps match our current resource coverage.

## Core concepts

| Concept | Meaning and owner |
| --- | --- |
| Workspace | Configuration, service endpoints, connection registry, metadata store, and input storage |
| Integration | Versioned code and metadata describing a service, supported input formats, configuration, resources, and capabilities |
| Connection | Stable ID for a configured account or source instance; owns its credential references, schedule, resource selection, and destination mapping |
| Artifact | Retained original input such as an uploaded export; records checksum, connection, format, receipt time, and acquisition method |
| Import | Attempt to parse and load a particular artifact using a particular parser version |
| Operation | A sync, import, backfill, or rebuild request, with an immutable configuration revision and one or more execution attempts |
| Dataset | A resource produced by a connection; records schema version, successful load reference, and coverage/freshness evidence |
| Analysis package | Optional dbt models and dashboard definitions, with declared source/model dependencies |

An integration may bundle default analysis packages. Analysis packages can also
combine several integrations, for example music listening plus health activity.
Do not force all data into a universal event table: use source tables and typed
domain models for listens, workouts, trips, and other concepts.

## Responsibility and state ownership

| Component | Responsibility |
| --- | --- |
| Workspace core | Validate configuration, resolve capabilities, register artifacts, request operations, reconcile observed status, and map product IDs to backend IDs |
| CLI / future HTTP API | Call shared application services; transport and presentation only |
| Secret resolver | Resolve credential references only for the operation that needs them; initially preserve the fnox/Proton Pass setup |
| Prefect adapter | Register/reconcile deployments and schedules, submit work, expose execution status and log links |
| dlt | Extraction/loading state, incremental cursors, normalization, merge behavior, and load packages |
| dbt | SQL transformations, dependency graph, model tests, and build artifacts |
| Metabase adapter | Resolve managed collections, questions, dashboards, and links |
| Metadata store | Product identities, configuration revisions, artifact/import records, run-stage summaries, backend references, and reconciliation timestamps |
| Artifact storage | Original files; local directory first, with an interface that can later support object storage |

Do not copy dlt cursors into a second state engine or build a second scheduler.
Connection schedules are desired configuration; Prefect is their execution backend.
Product status is a projection of execution/load/build evidence, not a replacement
for Prefect's execution history.

Separate request creation from backend submission with a stable request ID and
recoverable submission state. A timeout after submission must not blindly create
another run. Use backend idempotency where supported and reconcile ambiguous
submissions before retrying. Serialize writes sharing dlt state across all entry
points, including different deployments. Never claim exactly-once execution.

Each scheduled or externally triggered flow registers/adopts an operation using its
Prefect run ID before loading data. Retries create attempts, not unrelated operations.
CLI-originated requests carry their operation ID into that same entry point. Retain
n8n trigger compatibility during migration. Record cancellation and unknown backend
status separately from failure; metadata outages must not report a successful load
as failed or encourage blind replay. New executions require registration, and
completion evidence can be reconciled from durable load/build/run references.
Ingestion may partially commit before a run fails. Track completed resources/load
packages and resume safely; do not treat every failed run as an empty transaction.

Persist normalized non-secret configuration revisions separately from the mutable
configuration file. Executions use the revision attached to their deployment/request,
plus a pinned code/runtime reference. Secret values resolve at execution time;
credential rotation does not require saving old credentials in configuration history.
The runtime proof must establish how the worker obtains those credentials: current
`prefect.yaml` copies values into deployments, while this host uses fnox. Do not
assume the worker has the same vault session. Select and document a worker-side
resolver or explicit secret injection boundary before changing that behavior.

The initial CLI is a local administration client. The future server uses these same
application services; a remote CLI calls the HTTP API. Workers receive immutable
operation inputs and never edit desired configuration. A durable connection-scoped
work directory holds pending dlt packages; artifact paths must be accessible to the
worker through an explicit mount/storage binding. Prove restart recovery before
changing the existing worker's execution environment.

A new isolated runtime must be tested with the actual Prefect work pool. Current
Kubernetes access permits status reads but denied pod exec during this session;
worker/infrastructure changes may require the separate home-ops deployment path.
This is a delivery dependency to resolve early, not a reason to hide runtime work
inside a late application refactor.

## Acquisition and import contract

For file-based inputs:

    manual export / future browser download / future client
      -> register original artifact
      -> identify and validate format
      -> parse using the integration's parser
      -> load source records
      -> build affected models
      -> expose results and freshness

An export integration initially provides acquisition instructions and a CLI file
submission path. A watched directory and frontend upload are additional entry
points to the same artifact registration operation.

Future acquisition adapters produce the same artifact descriptor. Browser
navigation and credentials do not belong inside parsers. Delayed export requests
can record a remote request reference and resume retrieval later, including a
state that requires user authentication.

API sources can stream records directly to dlt. They do not have to materialize a
file for every response. Retained response payloads are an optional connector
capability with a defined retention policy.

Artifact checksums detect identical files. Source-specific record keys handle
logically overlapping exports. Artifact identity is scoped to a connection;
identical bytes from two accounts must not merge the accounts. Reprocessing is an
explicit operation and records the parser version and outcome. A failed attempt
must not mark an artifact successfully imported. Register byte receipt and import
attempts separately: a successfully loaded artifact remains imported if dbt fails,
and transformation-only retries must not reload it.

File parsers consume the selected artifact explicitly, without a directory-level
modification-time cursor filtering it out. Acquisition role (historical snapshot,
ongoing export, etc.) and cutoff options are metadata, not inferred from its storage
path. Replay identity includes connection, artifact, parser version, and relevant
options; an explicit replay can rerun a previous interpretation. The existing
scanner may remain as a compatibility acquisition adapter that registers artifacts.

Do not infer event histories from summary exports. Each resource declares whether
its records are events, current entities, or snapshots. For example, cumulative
play-time totals would be stored as dated observations, not invented gaming sessions.
Actual Switch format support requires a sample and a defined parser.

Track received time, source event time, source timezone/offset where available,
format version, integration version, and load/build references. Historical
coverage is declared or measured evidence; never infer completeness from one
successful poll. Deletion of a connection, deletion of retained artifacts, and
purging warehouse records are distinct explicit operations.

## Data correctness and evolution

- Default to retaining previously observed history. Upstream deletions, tombstones,
  corrected values, and retention expiry have explicit per-resource semantics.
  Merge tables preserve records, but do not preserve every prior field value.
- Distinguish backfill (fetch and merge a range), replay (parse retained inputs),
  model rebuild (rerun dbt), and destructive source replacement. A replacement
  cannot promise to restore data that has expired upstream. Require a recovery
  source/backup before planning destructive replacement.
- Preserve source namespaces and stable keys for existing connections. New
  connections use separate datasets and dlt state; canonical models expose a
  connection ID. Cross-account joins/deduplication require an explicit identity map.
- Separate integration-code, input-format, configuration, and dataset-schema versions.
  Changes to keys, cursor meaning, or table shape require a migration. Additive raw
  fields can be accepted when compatible; incompatible types/structures must produce
  a visible compatibility error rather than silently breaking downstream models.
- Parser fixes may change keys or remove erroneous rows. A merge-only replay cannot
  remove obsolete interpretations. Classify replay as merge-safe or requiring a
  scoped reconciliation/shadow rebuild, preserving original inputs and validating
  dependent models before promotion.
- Track freshness and coverage per resource, with an unknown state. Disabled or
  missing sources must not become numeric zero in cross-source analyses. The current
  `crossovers/daily.sql` coalesces absent joins to zero; update that interpretation
  alongside coverage-aware models, not merely the UI.
- Initially report models as built from observed input load versions and mark them
  stale if relevant inputs advance. A dbt-only concurrency lock does not stop new
  loads during a build. Do not promise an atomic cross-source snapshot; analyses
  requiring one need coordinated publication or a consistent snapshot design.
- Backup and restore metadata, warehouse/state, and original inputs before the first
  production migration. Application rollback alone cannot reverse schema/data changes.

## Configuration and code organization

Start with one project configuration file and per-integration Python modules.
Choose the filename and serialization format in milestone 1; use one format with
schema validation and a format version. No custom configuration language.

Desired configuration includes service bindings, stable connection IDs,
integration references, non-secret settings, secret references, enabled resources,
schedules/timezones, destination namespaces, retention choices, and enabled
analysis packages. Define and test precedence between shared configuration,
machine-local overrides, and explicitly supplied command options.

Store desired configuration on a dedicated persistent workspace volume, separate
from application code and worker checkouts. Git versioning can be an optional
export/review workflow; the app must not silently commit or push UI edits. The
local CLI and server use a revision check and atomic replacement so concurrent
file/UI edits cannot overwrite each other. Serialize the revision check and write
under one lock; atomic replacement alone is insufficient. A stale edit returns a conflict. An
immutable Git-managed installation can run the interface read-only and apply
externally reviewed configuration revisions.

Deployments reference immutable configuration revisions. Editing a file does not
silently change an already queued run. Config validation, execution application,
and observed status are distinct states exposed through CLI/UI.

A proposed layout, to refine while implementing the first two integrations:

    src/core/                 configuration, contracts, operations, metadata
    src/adapters/             prefect, secrets, artifact storage, metabase
    src/integrations/         listenbrainz, apple_health, ...
    src/cli/                  shared-operation command entry points
    dbt/                      domain and cross-source SQL models
    dashboards/               portable managed dashboard definitions

Keep extraction/parser functions usable without importing Prefect. Existing
`src/pipelines/` entry points remain compatibility wrappers during migration.
All bundled integrations can share one initial pinned application runtime; do not
promise per-plugin dependency isolation until it is implemented. Third-party
integrations with conflicting dependencies can later use separate runtimes.

## Milestone 0: establish baselines and prove the runtime boundary

Work:

- Inventory existing deployments, parameters, schedules, pipeline names, datasets,
  credential grants, data directories, and dbt/Metabase dependencies.
- Record source-specific behavior: GitHub API/export seeding; Apple Health's
  manual/automatic cutoff; browser snapshots; current refresh semantics.
- Capture schema and representative counts/keys without committing private data.
- Specify compatibility mapping and data-recovery procedure for each migrated connection.
- Inventory n8n/direct triggers as well as schedules; note deployment IDs they reference.
- Prove one synthetic load runs in the proposed isolated environment with worker-visible
  files and durable state, using the actual worker compatibility constraints. Include
  dependency locking (`uv.lock` is currently ignored), execution versioning, and the
  application/worker Prefect version relationship in this decision.
- Set up a small local development path: temporary Postgres schemas, fixture inputs,
  and documented commands. A pure parser/configuration test should not need a cluster.

Done when: the reference sources have detailed mappings, remaining sources have a
lightweight inventory, and the execution/storage boundary is demonstrated in an
isolated environment. Deep migration work for other sources waits until their turn.
Production inspection is read-only; no full refreshes are required.

## Milestone 1: core contracts and integration registry

Work:

- Define validated workspace/connection configuration and typed integration contracts.
- Separate configuration and secret references from source implementations.
- Implement a small explicit registry for the two bundled reference integrations.
- Add CLI operations for listing integrations/connections, validating configuration,
  and displaying resolved capabilities without printing credentials.
- Wrap ListenBrainz and Apple Health as reference integrations. Preserve the existing
  Apple Health Auto Export JSON format; native Health XML is a separate parser.
- Define stable per-connection state/dataset bindings. Pass account settings explicitly
  to sources; do not switch process-global environment variables between concurrent
  accounts. Resolve credentials inside each isolated execution context.

Done when: both reference integrations can be described and configured through the
same contracts, unknown settings fail clearly, and source code has no requirement
to import orchestration code. Treat this as a small enabling PR, not a product
milestone to expand with a general-purpose plugin framework. Proceed directly to
an executable API/file slice; validate abstractions through those executions.

## Milestone 2: execution and truthful status

Work:

- Implement metadata schema/migrations, stable operation IDs, and Prefect bindings.
- Add CLI operations to check a connection, request a supported sync, inspect status,
  and open the relevant execution/log link.
- Track ingestion and transformation outcomes independently. Preserve load success
  when downstream dbt fails, while clearly marking analysis as stale/failed.
- Reconcile file-declared schedules into Prefect through a reviewable plan/apply
  operation; detect drift rather than silently running duplicate schedules.
- Keep dlt state ownership and dbt concurrency rules explicit.
- Prove the reference integration runs with only its own source tables present.
  Build its self-contained domain models; leave cross-source models disabled until
  dependencies are present. The current source-based selector can include crossovers,
  so importing one source must not require preloading the entire personal warehouse.
- Build a versioned runtime with locked dependencies. Retain existing worker
  infrastructure initially, but stop installing application dependencies into its
  shared environment; validate a supported isolated process/container execution path.
- Record code, integration, and configuration revisions for each new execution.
- Register operations at the worker entry point so schedules, direct Prefect runs,
  and n8n triggers are visible even when they bypass the product API.
- Exercise two connections with overlapping source IDs and distinct credentials,
  verifying state, records, and retries stay independent. No full model union yet.
- Define request and attempt transitions, crash recovery, and cancellation behavior
  before implementing an event bus or background reconciliation service. Begin with
  explicit status refresh plus worker registration; add services only as needed.

Done when: one API integration runs through the core in an isolated test destination;
retries, ambiguous submission, same-connection concurrency, and dbt failure have
meaningful verification. A restart does not lose operation identity or hide failure.

## Milestone 3: manual import inbox

Work:

- Implement original-file storage and artifact/import metadata.
- Add CLI submission, format validation, import inspection, retry, and explicit replay.
- Use small synthetic Apple Health JSON fixtures to validate real parser behavior;
  include duplicate files, overlapping exports, invalid input, and changed parser versions.
- Preserve the current cutoff policy as explicit connection/import configuration.
  Keep historical/ongoing origin in artifact metadata after moving or hashing files;
  bypass filesystem time cursors for explicitly submitted/replayed artifacts.
- Test changed contents with an old timestamp, identical timestamps, interrupted
  loads, and the crash window after database commit but before status completion.
- Record checksums, input format, provenance, row/load outcomes, and retention policy.
- Ensure interrupted copies/uploads do not become valid artifacts; validate archive
  paths and resource limits before parsing archives when archive support is added.

Done when: manual input can be imported, repeated safely, diagnosed, and reprocessed
without invoking acquisition automation or resetting unrelated API cursors.

## Milestone 4: usable workspace interface

Work:

- Expose shared operations through an authenticated HTTP API and a small web interface.
- Cover the two reference integrations first: configured connections, manual import
  history, ingestion/model status, freshness, and supported actions.
- Provide CLI/UI configuration revision checks and atomic edits using the persistent
  workspace configuration location. Distinguish saved from applied configuration.
- Link to existing Prefect runs and Metabase dashboards. Portable dashboard generation
  and embedding are not prerequisites; assess embedding options separately later.
- Exercise the workflow on this installation's test setup, including expired
  credentials, invalid files, source outages, and a failed transformation.

Done when: connect -> sync/import -> inspect -> open existing analysis works through
one interface for one API and one file integration. This is the first usable product
checkpoint. Reassess the remaining scope based on actual use before expanding it.

## Milestone 5: migrate remaining integrations and models

Work:

- Migrate GitHub, bike share, and browser history to the same contracts.
- Give existing connections explicit legacy pipeline/dataset bindings to preserve
  warehouse history and cursors. Give new accounts distinct state/namespaces.
- Make account identity, timezone, and data-path assumptions configuration. Update
  dbt source/model wiring to combine enabled connections without account collisions.
- Handle deduplication between export and API data through documented source-specific
  rules. Do not assume separate keys or checksums solve cross-method overlap.
- Move hand-authored repeated deployment configuration into the core's deployment
  projection, with one authoritative schedule definition.
- Cut over one connection at a time: pause old schedules and external triggers,
  drain active/queued work, snapshot recoverable state/data, validate the new binding,
  compare outcomes, then enable the new entry points. Preserve externally referenced
  deployment IDs where possible or update n8n explicitly. Define rollback including
  any incompatible schema/state changes.

Done when: all current integrations work through the core, existing dashboards still
resolve, and the old paths can be retired without two schedulers or two writers
operating on the same connection. Production cutover is a separate explicit action.

## Milestone 6: analysis packages and portable installation

Work:

- Define analysis-package metadata, source dependencies, compatibility versions,
  dbt selectors, and optional dashboard assets.
- Keep a shared dbt project initially; derive dependencies from dbt artifacts where
  possible instead of maintaining a second SQL dependency graph.
- Implement portable dashboard definitions using logical names and resolved backend
  IDs. Do not put this installation's numeric database/dashboard IDs in packages.
- Reconcile only product-managed assets, preview changes, and preserve user-created
  dashboards. Prototype create/update behavior against the installed Metabase version.
- Make unavailable dependencies and stale transformed data visible; disable optional
  models with absent inputs and test a workspace containing only one integration.
- Define conflict ownership for managed dashboards edited in Metabase. Compare with
  the last applied definition, preserve conflicting edits, and allow detaching/forking
  an asset. Protecting only unmanaged dashboards is insufficient.
- Supply a convenient default deployment bundle and documented existing-service
  bindings. Separate warehouse, orchestration, and application metadata logically.
- Document and exercise upgrade, backup, restore, and data migration procedures.

Done when: music and one cross-source analysis can be enabled from declarations,
managed assets update without duplication, conflicts with managed-asset edits are
reported, and customized unmanaged assets survive. A fresh installation and restore
exercise must also pass before claiming portability.

## Later: automated acquisition and clients

Implement only after the manual import path is stable:

- Playwright acquisition for one concrete export-only service, with durable request,
  retrieval, authentication-needed, and artifact-created states.
- Watched folders and device uploads using the same artifact registration contract.
- Browser/mobile/desktop clients as producers. Specify their transport, identity,
  batching, and delivery acknowledgement when a real client is built.

Done when: manually supplied and automatically acquired versions of the same export
use the same parser, load behavior, import history, and analysis models. Do not claim
support for a service or export format until representative samples are available.

## Last: AI analysis

Build after data status and domain models are trustworthy:

- Read-only access to curated models, dataset descriptions, coverage, and freshness.
- Answers and reports that cite queries, time windows, datasets, and model versions.
- Explicit disclosure of missing coverage and stale inputs in generated analyses.
- Saved analyses/notebooks with reproducible query inputs and recorded generation time.
- Explicit opt-in before personal data is sent to an external model provider.

AI connector development assistance can be used earlier, with fixture-based
verification. It is separate from exposing personal data to an analysis agent.
An AI execution/administration agent is outside this initial scope.

## Validation and migration rules

- Use synthetic or sanitized fixtures in the repository. Never commit personal exports.
- Test contracts, independent accounts, merge/replay behavior, state continuity,
  partial failure, recovery, and data/model compatibility rather than UI snapshots
  or tests that merely duplicate declarations.
- Use temporary local destinations for development; use an isolated Postgres schema
  for database-specific integration checks before any production cutover.
- Track ingestion success, transformation success, last successful source check,
  artifact receipt time, and known historical coverage separately.
- Preview changes affecting deployments, destination mappings, refreshes, or managed
  dashboard assets. Destructive rebuilds/purges are explicit operations.
- Secret access is scoped to the executing connection/operation; configuration,
  status responses, fixtures, and normal logs contain no credential values.

## First implementation slice

Start with the reference-source baseline and runtime proof, then deliver milestone 1
as a small enabling PR. Follow immediately with one API execution and one explicit
artifact import (milestones 2 and 3). The first technical checkpoint is an executable
vertical slice with status, repeat import, independent accounts, and recoverable
failure. The first usable product checkpoint is milestone 4's small interface.

Keep each PR focused: configuration/registry; runtime and operation tracking; artifact
registration/parser adaptation; API/UI. Preserve the current entry points until an
explicit per-connection migration. Resolve storage/runtime availability before
investing in adapters that cannot execute in this environment.

Review points:

- After the runtime proof: keep or revise the Prefect execution mechanism.
- After the API/file slice: simplify any contract that neither reference needs.
- After the small interface: prioritize remaining connectors from actual use.
- Before dashboard packaging: verify installed Metabase behavior and editing conflicts.

Do not turn optional future features into first-release dependencies. The initial
release has one destination, one orchestrator, two integrations, manual imports,
truthful status, and links to existing analyses. AI and browser automation remain
later extensions.
