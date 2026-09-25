# Browser history (in progress)

Browsing history from Zen (Firefox-based) on the MacBook, plus the Arc history
from before the switch.

- **Status**: snapshots are being produced; pipeline written, not deployed yet.
- **Code**: [`src/sources/browser_history.py`](../../src/sources/browser_history.py), [`src/pipelines/browser_history.py`](../../src/pipelines/browser_history.py)
- **Raw schema**: `browser_history_data`

## Where the data comes from

Zen keeps history in `places.sqlite` in its profile, locked while the browser
runs. A mise task in the dotfiles snapshots it:

- **[`conf.d/zen-history.personal.toml`](https://github.com/h-wb/dotfiles/blob/main/conf.d/zen-history.personal.toml)**
  (MacBook only, `personal` env): task `mise run zen-history-snapshot` copies the
  DB + its WAL, checks it (`PRAGMA quick_check`), `VACUUM INTO` one clean file,
  gzips it into `~/Nextcloud/Hugo/Documents/data/browser/zen-places.sqlite.gz`
  (`sqlite` comes from mise). A launchd agent in the same file runs it every 6h
  (and at login); log in `~/Library/Logs/zen-history-snapshot.log`.
- `sqlite3 .backup` can't be used on the live DB: Zen holds an exclusive lock.
- **Arc seed**: `arc-history.json.gz` in the same folder, the "Export Chrome
  History" dump from Arc (one row per visit, 2025-01-07 → 2025-10-22, with
  transition type and referring visit).

Why a DB snapshot and not Firefox Sync or an extension: Sync is end-to-end
encrypted (needs account login + key derivation) and only keeps a recent window;
an extension would need signing and a webhook, and neither exposes
`moz_places_metadata` (time on page, typing), which only exists in the local DB.
Phone history would arrive in this DB through Sync (visits only) if enabled.

## Extraction

| Resource | Table | Key | Incremental |
|---|---|---|---|
| `zen_places` | `moz_places` (URL, title, visit count, frecency) | `id` | `last_visit_date` |
| `zen_historyvisits` | `moz_historyvisits` (one row per visit, `from_visit`, `visit_type`, `source`) | `id` | `visit_date` |
| `zen_places_metadata` | `moz_places_metadata` (`total_view_time`, `typing_time`, `key_presses`…) | `id` | `updated_at` |
| `zen_origins` | `moz_origins` | `id` | full |
| `zen_bookmarks` | `moz_bookmarks` | `id` | full |
| `arc_visits` | Arc export (`seed_arc=true` only) | `visitId` | replace |

Ids are local to the profile but stable, so they work as merge keys. Timestamps
are stored as the browsers store them: Firefox in microseconds (PRTime), the Arc
export in milliseconds; dbt converts.

## History

- History before 2025-01-07 doesn't exist anywhere: it's where the Arc profile
  started. Zen's pre-2025-10-23 visits are the Arc history imported into Zen
  (~3.7k visits fewer than the Arc export), so dbt should take Arc for that
  period and Zen after it.
- Firefox trims old history as the DB grows; the warehouse keeps it (merge).
