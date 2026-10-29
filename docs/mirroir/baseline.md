# Initial baseline

Recorded 2026-10-28 on the workspace implementation branch, based on main commit
`b5a6acd`. This separates repository evidence from live observations; it is not a
complete data backup or a production migration approval.

## Reference integrations

| Property | ListenBrainz | Apple Health |
| --- | --- | --- |
| Entrypoint | `src/pipelines/listenbrainz.py:load_listenbrainz` | `src/pipelines/apple_health.py:load_apple_health` |
| dlt pipeline name | `listenbrainz` | `apple_health` |
| Dataset | `listenbrainz_data` | `apple_health_data` |
| Source implementation | `src/sources/listenbrainz.py` | `src/sources/apple_health.py` |
| Config | Username and starting timestamp | Worker-visible directory and cutoff date |
| Source credential | `LISTENBRAINZ__ACCESS_TOKEN` | None |
| Schedule in repo | `0 0 * * *` | `0 0 * * 0` |
| External trigger | None documented | n8n after saving an export |
| Post-load action | `refresh_models("listenbrainz")` | `refresh_models("apple_health")` |

ListenBrainz merges on `(listened_at, recording_msid)` and advances an event-time
cursor. Its paginator deliberately overlaps one second to retain ties, and pauses
between pages. Older backfills and upstream deletions are not reconciled by normal
incremental sync. The current `full_refresh` truncates data and resets state.
Preserve these identities and document any semantic change separately.

Apple Health reads Health Auto Export JSON, not Apple's native XML. It scans
`**/*.json` incrementally by file modification time. A `/manual/` path selects rows
before the configured cutoff; other paths select rows at/after the cutoff using
the source timestamp's local date prefix. Missing metric `source` becomes `unknown`.
Keys are `(metric_name, date, source)` for metrics, `id` for workouts/state of mind,
and `(displayText, start)` for medications. There is no durable artifact/import
registry, and the scanner is not an explicit replay interface.

Both flows load before rebuilding dbt models. dbt failures are caught and logged,
so successful Prefect flow status does not prove fresh analyses. The global `dbt`
concurrency limit serializes builds, not source loads. Source-based selectors can
include crossovers requiring other source tables. Current dbt source schemas are
hardcoded and some account/timezone assumptions live in `dbt_project.yml`.

## Other existing entry points

| Source | Pipeline / dataset | Migration consideration |
| --- | --- | --- |
| GitHub | `github` / `github_data` | API and export resources; seed mode truncates selected resources; URL-based union in models |
| Bike share | `bikeshare` / `bikeshare_data` | Member ID/token and trip child tables |
| Browser history | `browser_history` / `browser_history_data` | Worker-mounted browser snapshots; existing refresh truncates source data |
| Nightly dbt | `run_dbt` | Builds every model; current nightly schedule 03:00 |

This table is a lightweight repository inventory. Detailed data/key/cursor and
current deployment comparisons are required before migrating each source. Existing
dashboard IDs are documented in source pages; portable dashboard registration is
not implemented.

## Runtime observations and constraints

Read-only Kubernetes inspection during this slice found one ready worker replica:

- Image: `prefecthq/prefect:3.9.0-python3.12` with digest
  `sha256:9f4fb20a431f0aba81bbcede83d5c08648aa769faf0ec47e989e9d370270129e`.
- Mounts: `/root/.prefect/data` and `/root/.prefect`.
- The repo declares `local-pool`; pull steps clone `main` and run `uv pip install .`.
- `prefect.yaml` records `prefect-version: 3.8.6`; this is distinct from the observed
  worker image version and not evidence that the image is running that older version.
- Earlier session checks verified authenticated API reads via the internal service.
  Pod exec was denied for the Paseo service account. No worker mutation was attempted.
- Existing deployment definitions inject credential values into job environment
  variables. Host-side fnox access does not establish worker-side vault access.

The local Python 3.12 validation environment uses Prefect 3.9.0, dlt 1.31.0,
Pydantic 2.14.1, and DuckDB 2.0.0 from the committed lockfile. Production still uses
its existing installation procedure; committing a lockfile alone does not change it.

## Outstanding production baseline and runtime proof

- Record live deployment IDs, schedules, queued/active runs, and external trigger
  references immediately before cutover; repository declarations are not live state.
- Capture recoverable warehouse/state backups and private comparison metrics outside
  Git. Historical documentation counts are not a fresh database baseline.
- Prove isolated execution through the actual Prefect work pool, including mounted
  inputs, pending-package durability, cancellation, and credential injection.
- Choose the home-ops change or existing work-pool facility that supplies isolation;
  validate against the observed image and permissions.
- Verify Postgres merge/DDL behavior and model compatibility in an isolated schema.

The synthetic local tests establish source compatibility and committed-state
restoration. They do not close these production gates.
