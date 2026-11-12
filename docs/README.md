# Pipelines documentation

Personal data warehouse: every service I use, mirrored into one Postgres so it
outlives the service's own retention and can be joined across sources.

Miroir development: [implementation guide and decision log](miroir/README.md).
Roadmap: [Miroir implementation plan](product-plan.md).

```
 source service ──(API / export / file drop)──▶ dlt source ──▶ Postgres  <source>_data   (raw)
                                                                   │
                                                dbt (after each load + nightly 03:00)
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

Since 2026-11-11 every source loads through a Miroir connection bound to its
original raw schema (decision 021); the legacy per-source flows are retired.

| Source | Data | Miroir connection, schedule (UTC) | Raw schema | dbt | Dashboards |
|---|---|---|---|---|---|
| [ListenBrainz](sources/listenbrainz.md) | every listen (Spotify, Plex, Last.fm history) | `listenbrainz`, daily 00:00 | `listenbrainz_data` | `music`, `crossovers` | Music (2), crossovers |
| [GitHub](sources/github.md) | commits, PRs, issues, comments, releases, stars, events, contribution calendar | `github`, daily 01:00 | `github_data` | `github` | GitHub (30) |
| [Apple Health](sources/apple-health.md) | metrics, workouts, state of mind, medications | `health`, on each export (receiving URL) | `apple_health_data` | `health`, `crossovers` | Health (24), crossovers |
| [Bike Share Toronto](sources/bikeshare.md) | every rental | `bikeshare`, daily 00:00 | `bikeshare_data` | `biking`, `crossovers` | Biking (3), Rides (26) |
| [Browser history](sources/browser-history.md) | Zen (Firefox) visits, time on page, typing | retired 2026-11-11, to be redone in Miroir | `browser_history_data` (kept) | `browser` | Browser (31) |
| Dawarich | location history | in progress, not deployed | `dawarich_data` | – | – |

Crossover dashboards (collection "Crossovers"): Music & Heart (25), Rides (26),
Late nights (27), Correlations (28), Year in review (29). Each Miroir load builds
its connection's analysis into its own `miroir_<analysis>_…` schemas; the
dashboards above read the original mart schemas, which the legacy `run_dbt`
deployment still rebuilds from the raw schemas daily at 03:00.

## How a run works

- **Miroir** connections run as Prefect flow runs on the `miroir-k8s` pool, each
  in its own Kubernetes Job from the pinned image; their deployments come from
  the Miroir configuration (see [Miroir](miroir/README.md)).
- **Prefect**'s process worker (`local-pool`) still runs `run_dbt` (and the
  dawarich flows, from another repo). Its pull step clones this repo's `main` and
  `uv pip install .` **into the worker's own environment**, so pushing to `main`
  changes the next `run_dbt`. Its definition is in [`prefect.yaml`](../prefect.yaml)
  and only changes with `mise run deploy`.
- **Warehouse**: CNPG cluster `postgres-metabase` (automation namespace), database
  `metabase`. Only reachable in-cluster; `kubectl port-forward` drops under bulk
  loads, so one-off loads run on the worker (see [operations](operations.md)).
- **File drops**: Miroir flow runs mount the NAS share `Documents/data` at
  `/miroir/data` (the legacy worker at `/root/.prefect/data`). The same folder is
  `Hugo/Documents/data` in Nextcloud, so anything synced there from a laptop
  (browser snapshots, exports) can feed a Miroir watched folder.
- **Metabase** reads the warehouse as database id 2; dashboards are built with
  the `mb` CLI (query-builder cards, date + group-by filters).

## Secrets and config

- Every credential lives in the Proton Pass **`Automation General`** vault: source
  tokens (`listenbrainz`, `bikeshare`, `github-pipelines`; the Miroir connections
  hold their own copies in Miroir's encrypted store) and operator copies
  (`prefect`, `postgres-metabase`), which mirror BWS/CNPG until operator tasks
  read those sources directly. References map them to env vars in
  [`fnox.toml`](../fnox.toml). The `miroir` installation item is still
  unprovisioned; its metadata password and existing encryption key must be
  supplied before infrastructure development/deployment tasks can use it.
  Mise injects only the keys listed in each task's `secrets = [...]` grant, including warehouse and
  Prefect credentials only where needed.
  Run `mise run pass:login` first (macOS Keychain or Paseo's `PROTON_PASS_TOKEN`).
  `mise secrets ls` lists names and grants without fetching values; ad-hoc
  commands use `mise x --secrets KEY -- <command>`.
- Shared non-secret config is in [`mise.toml`](../mise.toml); machine overrides
  go in gitignored `mise.local.toml`. No repo mise environment is selected, so
  the machine keeps its dotfiles layers (including pod/Paseo settings).
- In-cluster hosts select the tracked [`fnox.cluster.toml`](../fnox.cluster.toml)
  profile with the following in gitignored `mise.local.toml`. It changes only
  `PREFECT_API_URL` to the internal Kubernetes service; other secrets still come
  from Proton Pass. Without this selection, the default profile uses the vault URL.
  Do not also set `PREFECT_API_URL` under mise `[env]`: it conflicts with the
  task secret grant.

  ```toml
  [secrets.fnox]
  profile = "cluster"
  ```
- `prefect.yaml` copies the env vars `run_dbt` needs into its deployment at
  deploy time.
- Prefect must stay a **range** (`prefect>=3.4,<4`) in `pyproject.toml`: an exact
  pin makes every run reinstall a different Prefect into the shared worker and
  crash the runs after it.

## Adding a source

1. A connector package in `src/integrations/<name>/` (settings, credentials,
   check, resources, inputs, dlt source: extract only, raw records, merge keys,
   incremental cursor with an overlap), listed in `registry.py` (decision 021).
2. dbt: raw tables in `dbt/models/staging/_sources.yml`, `stg_<source>__*` views,
   marts in a `dbt/models/<topic>/` schema, and an analysis package in
   `src/analyses/catalog.py`.
3. Create the connection in the Miroir interface (credentials go to its
   encrypted store), then sync it once.
4. A page in `docs/sources/` from the template below, and a row in the table above.

Source page template: status & schedule · where the data comes from (and any
code outside this repo) · extraction (resources, keys, cursors) · tables → dbt →
dashboards · secrets & config · backfills and history · quirks.

See also: [operations runbook](operations.md).
