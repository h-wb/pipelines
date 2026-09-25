# ListenBrainz

Every listen, from 2019-06 on. ListenBrainz is the hub: other players scrobble
into it and this pipeline mirrors it.

- **Schedule**: daily 00:00 UTC (`load-listenbrainz/load_listenbrainz`)
- **Code**: [`src/sources/listenbrainz.py`](../../src/sources/listenbrainz.py), [`src/pipelines/listenbrainz.py`](../../src/pipelines/listenbrainz.py)
- **Raw schema**: `listenbrainz_data.listens`
- **dbt**: `staging.stg_listenbrainz__listens` → `music.*`, `crossovers.*`
- **Dashboards**: Music (2), Music & Heart (25), Late nights (27), Year in review (29)

## Where the data comes from

| Period | Submitted by (`submission_client`) |
|---|---|
| 2019-06 → 2020-10 | ListenBrainz Archive Importer (old Last.fm history) |
| 2020-10 → 2025-09 | ListenBrainz lastfm importer v2 |
| 2025-09 → now | multi-scrobbler (Spotify + Plex), plus a few BrainzPlayer listens |

**multi-scrobbler** runs in the cluster ([home-ops `media/multiscrobbler`](https://github.com/h-wb/home-ops/tree/main/kubernetes/apps/main/media/multiscrobbler))
and polls Spotify/Plex, scrobbling to ListenBrainz and Last.fm. Its
`/api/metrics` is scraped by Prometheus; `MultiScrobblerSourceIssue` /
`MultiScrobblerClientIssue` alert when a source stops polling or a client needs
re-auth (a lost Spotify connection used to go unnoticed for weeks).

## Extraction

One resource, `listens`, over `GET /1/user/{username}/listens`:

- merge on `(listened_at, recording_msid)`: `listened_at` alone isn't unique.
- incremental on `listened_at`, starting at `LISTENBRAINZ__START_DATE`.
- custom paginator: `min_ts` is exclusive, so paging from a page's newest
  timestamp (or resuming at the cursor) skipped other listens in the same second
  (~1,600 seconds in the history hold 2+ listens). It always asks one second
  earlier and lets the merge key dedupe; it stops on a short page.
- pages are paced 1.5 s apart: back-to-back paging (a full refresh is ~124
  pages) gets ListenBrainz's HTML bot check instead of JSON.

## Secrets and config

`LISTENBRAINZ__ACCESS_TOKEN` (Proton Pass `dlt/listenbrainz`),
`LISTENBRAINZ__USERNAME`, `LISTENBRAINZ__START_DATE` (mise config).

## History and cleanups

- **2026-09-23 dedupe.** From 2025-12-13 to 2026-03-07 ListenBrainz's own Spotify
  connector ran alongside multi-scrobbler, doubling most plays (same track,
  timestamps seconds apart). 3,426 duplicates were deleted *in ListenBrainz*
  (`POST /1/delete-listen`), keeping the multi-scrobbler copy; then a full refresh.
  606 connector-only listens were kept: 180 during multi-scrobbler outages and
  ~425 short plays multi-scrobbler didn't count (it scrobbles at 50% or 4 min;
  the connector at ~30 s).
- **Gap 2026-08-24 → 2026-09-22**: multi-scrobbler lost its Spotify session;
  nothing was scrobbled. To be backfilled from the Spotify data export
  (import only plays with no listen within ~2 minutes).
- After the cleanup the warehouse matched ListenBrainz exactly (123,417 listens).

## Quirks

- Deletions in ListenBrainz are applied "shortly after the hour" and the listens
  endpoint caches by URL: vary the query to see fresh results.
- The incremental load never sees deletions or listens backfilled below the
  cursor: run a full refresh after either.
