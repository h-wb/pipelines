# Apple Health

Health metrics (steps, heart rate, energy, sleep, body…), workouts, state of mind
and medications from the iPhone / Apple Watch.

- **Schedule**: weekly, Sunday 00:00 UTC (`load-apple-health/load_apple_health`)
- **Code**: [`src/sources/apple_health.py`](../../src/sources/apple_health.py), [`src/pipelines/apple_health.py`](../../src/pipelines/apple_health.py)
- **Raw schema**: `apple_health_data.{metrics, workouts, state_of_mind, medications}`
- **dbt**: `staging.stg_health__{metrics, sleep, workouts}` → `health.{daily_summary, workout_summary}`, `crossovers.*`
- **Dashboards**: Health (24), Music & Heart (25), Correlations (28), Year in review (29)

## Where the data comes from

1. The iOS app **Health Auto Export** POSTs JSON exports on a schedule to an n8n
   webhook.
2. The n8n workflow **"Apple Health → Files: Ingest Exports"** writes each POST
   as-is to `/documents/health/<automation>-<timestamp>.json` (kept deliberately
   minimal: no parsing or date logic in n8n).
3. That folder is the NAS share `Documents/data`, mounted on the Prefect worker at
   `/root/.prefect/data` → `APPLE_HEALTH__DATA_DIR=/root/.prefect/data/health`.

## Extraction

dlt's `filesystem` source globs `**/*.json`, incremental on file modification
date, so each file is read once. Every file's `data` object splits into:

| Table | Key |
|---|---|
| `metrics` (one row per metric data point; `metric_name`, `units`) | `(metric_name, date, source)`; missing `source` becomes `"unknown"` |
| `workouts` | `id` |
| `state_of_mind` | `id` |
| `medications` | `(displayText, start)` |

**Manual vs automatic.** Files under `manual/` are the one-off bulk export and
only contribute rows dated before `APPLE_HEALTH__CUTOFF` (2026-06-04); all other
files only contribute rows from the cutoff on. That stops the bulk export and the
automatic exports from overlapping.

## Secrets and config

No secrets. `APPLE_HEALTH__DATA_DIR`, `APPLE_HEALTH__CUTOFF` in `mise.prod.toml`.

## Quirks

- n8n splits `N8N_RESTRICT_FILE_ACCESS_TO` on `;`. A `:` separator silently
  blocked the writes from 2026-06-07 to 2026-09-22.
- Filenames only need to be unique: the merge keys dedupe overlapping exports.
