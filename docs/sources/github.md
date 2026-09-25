# GitHub

My GitHub footprint: everything in the repos I own (any author, bots included)
plus my activity elsewhere, from 2017 on.

- **Schedule**: daily 01:00 UTC (`load-github/load_github`)
- **Code**: [`src/sources/github.py`](../../src/sources/github.py), [`src/pipelines/github.py`](../../src/pipelines/github.py)
- **Raw schema**: `github_data`
- **dbt**: `staging.stg_github__*` → `github.{commits, pull_requests, issues, issue_comments, releases, contributions}`
- **Dashboard**: GitHub (30): Overview / Pull requests / Stars & activity tabs, contribution-graph heatmap

## Extraction

Records are loaded as the API returns them. Account level:

| Resource | Endpoint | Key | Notes |
|---|---|---|---|
| `repos` | `/user/repos` (owner, collaborator, org member) | `id` | |
| `stars` | `/user/starred` (`star+json` for `starred_at`) | `repo__id` | merge: unstars keep their row |
| `events` | `/users/{u}/events` | `id` | GitHub keeps ~90 days / 300 events: the daily run is the archive; `payload` kept as one json column |
| `contributions_daily` | GraphQL `contributionsCollection`, one year per query | `date` | the profile heatmap, since 2016 |

Repo level, for every repo I own (forks included):

| Resource | Endpoint | Key | Incremental |
|---|---|---|---|
| `commits` | `/repos/{r}/branches` → `/repos/{r}/commits?sha=<branch>&since=` → `/repos/{r}/commits/{sha}` for line stats (`files` patches dropped) | `html_url` | `since` |
| `pull_requests` | `/repos/{r}/pulls?state=all&sort=updated` | `html_url` | walk until past the window |
| `issues` | `/repos/{r}/issues?since=` (also lists PRs; dbt drops them) | `html_url` | `since` |
| `issue_comments` | `/repos/{r}/issues/comments?since=` | `html_url` | `since` |
| `releases` | `/repos/{r}/releases` | `html_url` | full every run |

My activity in other people's repos, via search (30 req/min, paced; date ranges
split in half past the 1,000-result cap):

| Resource | Query |
|---|---|
| `external_commits` | `author:<me> -user:<me>` on `/search/commits` (default branches only), then `/commits/{sha}` for stats |
| `external_issues` | `is:issue` and `is:pull-request` `author:<me> -user:<me>` on `/search/issues` |

Every incremental window re-reads 3 days so late updates (merges, edits) are merged.

## Export seed (one-shot)

History came from a GitHub **account-data export** (Settings → Account → Export),
loaded with `load_github(export_dir=…)` into raw `export_*` tables:
`export_commits` (`git log --all --numstat` of every bundled repo),
`export_pull_requests`, `export_issues`, `export_issue_comments`,
`export_releases`, `export_bots`. The same run starts the repo-level API
resources at the export date. dbt unions export + API per record type on the web
URL (API row wins; export fills fields the API lacks). The API alone can rebuild
all of it (checked: 100% URL overlap), the export mainly saves ~12k per-commit
calls for line stats. Reviews, issue events and attachments from the export
weren't kept (mostly bots). The archive itself stays on the laptop.

## What "mine" means (dbt)

- `is_mine` on commits: author email in `GITHUB__AUTHOR_EMAILS` (a JSON list, in
  Proton Pass `dlt/github/author-emails`, passed to the **dbt** deployment only),
  or my login, or my name on commits made with no email configured. Git emails
  are often not linked to the account: ~1,000 commits (and all 2017–2021 school
  work) only show up this way.
- `is_bot`: GitHub user type `Bot`, a `[bot]` suffix, or the export's bot list.
- `is_fork_repo`: forks carry upstream history; filter `is_mine or not is_fork_repo`.

## Secrets and config

`GITHUB__ACCESS_TOKEN`: fine-grained PAT (Proton Pass `dlt/github`), all own
repos, read-only: Contents, Metadata, Issues, Pull requests; account: Starring.
Fine-grained tokens can't see private events or repos owned by others (old
classmate repos). `GITHUB__USERNAME` in mise config.

## Quirks

- Commit search only indexes default branches; own repos use the branch walk.
- Issue search now requires `is:issue` / `is:pull-request` in the query (422 otherwise).
- `contributions_daily.date` arrives as text; `github.contributions` casts it.
- The heatmap card is a native-SQL table with fixed week columns: Metabase can't
  color pivot cells.
