# Apple Health

Health metrics (steps, heart rate, energy, sleep, body…), workouts, state of mind
and medications from the iPhone / Apple Watch.

- **Schedule**: Miroir connection `health`, on each export, through the connection's receiving URL
- **Code**: connector [`src/integrations/apple_health/`](../../src/integrations/apple_health/) (legacy flow retired 2026-11-11)
- **Raw schema**: `apple_health_data.{metrics, workouts, state_of_mind, medications}`
- **dbt**: `staging.stg_health__{metrics, sleep, workouts}` → `health.{daily_summary, workout_summary}`, `crossovers.*`
- **Dashboards**: Health (24), Music & Heart (25), Correlations (28), Year in review (29)

## Where the data comes from

The iOS app **Health Auto Export** POSTs each JSON export to the `health`
connection's receiving URL (`POST /api/connections/health/receive`, with its
connection-scoped delivery token). Miroir stores the file as an artifact and
imports it (decisions 017, 021). Until 2026-11-11 an n8n workflow wrote the
exports to the NAS for a legacy flow; that path is retired and archived.

## Extraction

Each received file is imported once by content (identical bytes are not imported
again). Its `data` object splits into:

| Table | Key |
|---|---|
| `metrics` (one row per metric data point; `metric_name`, `units`) | `(metric_name, date, source)`; missing `source` becomes `"unknown"` |
| `workouts` | `id` |
| `state_of_mind` | `id` |
| `medications` | `(displayText, start)` |

**Historical vs ongoing.** Each import has a `role`: `historical` files (the
one-off bulk export) only contribute rows dated before the connection's cutoff
(2026-06-04); `ongoing` files, the default for received exports, only contribute
rows from the cutoff on. That stops the bulk export and the automatic exports
from overlapping.

## Secrets and config

The delivery token is managed on the connection's setup page; the cutoff is a
connection setting. No other secrets.

## Quirks

- Exports between 2026-06-07 and 2026-09-22 never arrived: the old n8n path
  silently blocked its writes (`N8N_RESTRICT_FILE_ACCESS_TO` splits on `;`).
- Filenames only need to be unique: the merge keys dedupe overlapping exports.
