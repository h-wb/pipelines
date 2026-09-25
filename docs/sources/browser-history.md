# Browser history

Browsing history from Zen (Firefox-based) on the MacBook, from 2025-01-07 on,
including how long each page was in view and how much I typed on it.

- **Schedule**: daily 02:00 UTC (`load-browser-history/load_browser_history`); snapshots every 6h
- **Code**: [`src/sources/browser_history.py`](../../src/sources/browser_history.py), [`src/pipelines/browser_history.py`](../../src/pipelines/browser_history.py)
- **Raw schema**: `browser_history_data.zen_*`
- **dbt**: `staging.stg_browser__{visits, page_interactions}` → `browser.{visits, domains_daily}`
- **Dashboard**: Browser (31): Overview (pages, hours in view, top sites, how I got there) / When (weekday × hour heatmap, hour and weekday charts, recent visits); Date, Group by and Site filters

## Where the data comes from

Zen keeps history in `places.sqlite` in its profile, locked while the browser
runs. A mise task in the dotfiles snapshots it:

- **[`conf.d/zen-history.personal.toml`](https://github.com/h-wb/dotfiles/blob/main/conf.d/zen-history.personal.toml)**
  (MacBook only, `personal` env): task `mise run zen-history-snapshot` copies the
  DB + its WAL, checks it (`PRAGMA quick_check`), `VACUUM INTO` one clean file and
  gzips it to `~/Nextcloud/Hugo/Documents/data/browser/zen-places.sqlite.gz`
  (`sqlite` comes from mise). A launchd agent in the same file runs it every 6h
  and at login; log in `~/Library/Logs/zen-history-snapshot.log`.
- `sqlite3 .backup` can't be used on the live DB: Zen holds an exclusive lock.
- Nextcloud's `Hugo/Documents/data` is the NAS share the Prefect worker mounts at
  `/root/.prefect/data`, so the pipeline reads
  `BROWSER_HISTORY__DATA_DIR=/root/.prefect/data/browser` as a plain file.

The Arc history from before the switch (2025-01-07 → 2025-10-22) was imported
into Zen, so it's part of the same DB (visits with `source = 3` "restore" also
come from that import). History before 2025-01-07 doesn't exist anywhere.

Why a DB snapshot and not Firefox Sync or an extension: Sync is end-to-end
encrypted (needs account login + key derivation) and only keeps a recent window;
an extension would need signing and a webhook, and neither exposes
`moz_places_metadata` (time in view, typing), which only exists in the local DB.
Phone history would arrive in this DB through Sync (visits only) if enabled.

## Extraction

The snapshot is decompressed once per run and read read-only; tables load raw.

| Resource | Table | Key | Incremental |
|---|---|---|---|
| `zen_places` | `moz_places` (URL, title, visit count, frecency) | `id` | `last_visit_date` |
| `zen_historyvisits` | `moz_historyvisits` (`place_id`, `from_visit`, `visit_type`, `source`) | `id` | `visit_date` |
| `zen_places_metadata` | `moz_places_metadata` (`total_view_time`, `typing_time`, `key_presses`, scrolling) | `id` | `updated_at` |
| `zen_origins` | `moz_origins` (scheme + host) | `id` | full |
| `zen_bookmarks` | `moz_bookmarks` | `id` | full |

Ids are local to the profile but stable, so they work as merge keys. Text that
isn't valid UTF-8 (page descriptions as sites sent them) is decoded with
replacement characters rather than failing the run.

## dbt

- Units: visit times are **microseconds** (PRTime); `moz_places_metadata`
  timestamps and durations are **milliseconds**. Staging converts both.
- `browser.visits`: one row per visit with URL, title, domain (`moz_origins` host,
  `www.` stripped), readable `visit_type`, `source`, local `day`/`hour`, and
  `is_navigation` (false for redirect hops, reloads, embeds, framed links).
- `browser.domains_daily`: per site per local day, navigations plus minutes in
  view, minutes typing and key presses.

## Quirks

- Firefox trims old history as the DB grows; the warehouse keeps it (merge).
- A new snapshot only reaches the worker once the Nextcloud client uploads it.
  When the client lags, copy it in directly:
  `kubectl -n automation exec -i deploy/prefect-worker -- sh -c 'cat > /root/.prefect/data/browser/zen-places.sqlite.gz' < ~/Nextcloud/Hugo/Documents/data/browser/zen-places.sqlite.gz`
