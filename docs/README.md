# Pipelines documentation

Personal data warehouse: every service I use, mirrored into one Postgres so it
outlives the service's own retention and can be joined across sources.

```
 source service ──(API / export / file drop)──▶ dlt source ──▶ Postgres  <source>_data   (raw)
                                                                   │
                                                          dbt (nightly 03:00)
                                                                   ▼
                                                     staging views + mart schemas ──▶ Metabase
```

- **Extract raw, transform in dbt.** dlt sources yield records as the service
  returns them (dlt flattens nested JSON into columns / child tables). Anything
  that interprets the data (who is "me", bots, units, time zones, dedup across
  sources) lives in `dbt/`.
- **Merge, don't append.** Every resource has a primary key and
  `write_disposition="merge"`, so re-fetching a window is harmless. Incremental
  cursors re-read a small overlap to catch late updates.
- **The warehouse keeps what services forget.** Merges never delete, so rows a
  service expires (GitHub's 90-day event feed, Firefox's history trimming) stay.

## Sources

| Source | Data | Schedule (UTC) | Raw schema | dbt | Dashboards |
|---|---|---|---|---|---|
| [ListenBrainz](sources/listenbrainz.md) | every listen (Spotify, Plex, Last.fm history) | daily 00:00 | `listenbrainz_data` | `music`, `crossovers` | Music (2), crossovers |
| [GitHub](sources/github.md) | commits, PRs, issues, comments, releases, stars, events, contribution calendar | daily 01:00 | `github_data` | `github` | GitHub (30) |
| [Apple Health](sources/apple-health.md) | metrics, workouts, state of mind, medications | weekly Sun 00:00 | `apple_health_data` | `health`, `crossovers` | Health (24), crossovers |
| [Bike Share Toronto](sources/bikeshare.md) | every rental | daily 00:00 | `bikeshare_data` | `biking`, `crossovers` | Biking (3), Rides (26) |
| [Browser history](sources/browser-history.md) | Zen (Firefox) visits, time on page, Arc history seed | in progress | `browser_history_data` | – | – |
| Dawarich | location history | in progress, not deployed | `dawarich_data` | – | – |

Crossover dashboards (collection "Crossovers"): Music & Heart (25), Rides (26),
Late nights (27), Correlations (28), Year in review (29). dbt `run_dbt` runs
daily at 03:00, after every load.

## How a run works

- **Prefect** ([home-ops `automation/prefect`](https://github.com/h-wb/home-ops/tree/main/kubernetes/apps/main/automation/prefect))
  has one process worker (`local-pool`). Each deployment's pull step clones this
  repo's `main` and `uv pip install .` **into the worker's own environment**, so
  pushing to `main` is deploying code. Deployment definitions (schedule, params,
  env) are in [`prefect.yaml`](../prefect.yaml) and only change with `mise run deploy`.
- **Warehouse**: CNPG cluster `postgres-metabase` (automation namespace), database
  `metabase`. Only reachable in-cluster; `kubectl port-forward` drops under bulk
  loads, so one-off loads run on the worker (see [operations](operations.md)).
- **File drops**: the worker mounts the NAS share `Documents/data` at
  `/root/.prefect/data`. The same folder is `Hugo/Documents/data` in Nextcloud,
  so anything synced there from a laptop (browser snapshots, exports) is readable
  by flows as plain files.
- **Metabase** reads the warehouse as database id 2; dashboards are built with
  the `mb` CLI (query-builder cards, date + group-by filters).

## Secrets and config

- Secrets live in the Proton Pass vault **`dlt`** (one item per service) and are
  mapped to env vars in [`fnox.toml`](../fnox.toml) (references only). Every mise
  task runs under `fnox --if-missing error exec`; the `prod` profile adds the
  warehouse and Prefect credentials.
- Non-secret config is in [`mise.toml`](../mise.toml) (local defaults) and
  [`mise.prod.toml`](../mise.prod.toml) (prod, the default env via `.miserc.toml`).
- dlt resolves `<PIPELINE_NAME>__<KEY>` env vars (e.g. `GITHUB__ACCESS_TOKEN`),
  so the env prefix must match the pipeline name.
- `prefect.yaml` copies the needed env vars into each deployment at deploy time.
- Prefect must stay a **range** (`prefect>=3.4,<4`) in `pyproject.toml`: an exact
  pin makes every run reinstall a different Prefect into the shared worker and
  crash the runs after it.

## Adding a source

1. `src/sources/<name>.py`: extract only, raw records, merge keys, incremental
   cursor with an overlap. `src/pipelines/<name>.py`: the `@flow`, with
   `full_refresh` using `refresh="drop_data"` (truncate; dbt views depend on the tables).
2. Secrets: a field in the Proton Pass `dlt` vault + a line in `fnox.toml`;
   config in `mise*.toml`; env vars in the deployment block of `prefect.yaml`.
3. `mise run deploy`, then a first run with `mise run prefect -- deployment run <flow>/<deployment> --watch`.
4. dbt: raw tables in `dbt/models/staging/_sources.yml`, `stg_<source>__*` views,
   marts in a `dbt/models/<topic>/` schema.
5. A page in `docs/sources/` from the template below, and a row in the table above.

Source page template: status & schedule · where the data comes from (and any
code outside this repo) · extraction (resources, keys, cursors) · tables → dbt →
dashboards · secrets & config · backfills and history · quirks.

See also: [operations runbook](operations.md).
