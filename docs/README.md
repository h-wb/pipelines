---
area: "[[Homelab]]"
categories:
  - "[[Resources]]"
reviewed: 2026-11-24
summary: The index of this repo's pages and of its connectors, with what each reads, its collector source, tables and packages, and how secrets reach a checkout.
topics:
  - "[[Warehouse]]"
  - "[[Device collector]]"
created: 2026-09-24T22:01
---

# Documentation

Miroir keeps every source I use in one Postgres, so it outlives each service's
own retention and can be joined across sources. Why, and how it fits with the
other systems: the handbook's
[Warehouse](https://github.com/h-wb/handbook/blob/main/Systems/Warehouse.md) and
[Device collector](https://github.com/h-wb/handbook/blob/main/Systems/Device%20collector.md).
What is deployed and how to operate it:
[home-ops](https://github.com/h-wb/home-ops/blob/main/kubernetes/apps/main/automation/miroir/README.md).

## Pages

| Page | What it holds |
| --- | --- |
| [Miroir](miroir/README.md) | What it is, the words, the rules, the commands, development; the reference pages beside it |
| [Flows](miroir/flows.md) | How data moves in and out, one diagram per flow |
| [Architecture](architecture.md) | Which part of the code does what, who may import whom, where a new thing goes |
| [Decisions](miroir/decisions.md) | Why it is built this way, numbered |
| [The package split](decisions/2026-11-25-package-split.md) | The parts the code is split into, their contracts and the order of the move (2026-11-25) |
| [The Prefect version contract](decisions/2026-11-27-prefect-version-contract.md) | Which Prefect servers the adapter accepts, the contract tests in CI, the order of upgrades (2026-11-27) |
| [Writing an integration](miroir/integrations.md) | How a connector is written and tested |
| [Observability](observability.md) | How Miroir tells a quiet source from a broken one; metrics and alert rules |
| [Operations](operations.md) | Adding a device source or an analysis, commands from a checkout, symptoms and failures seen |
| [Releases](releases.md) | How the collector is versioned and released |
| [The collector](../collector/README.md) | Install, configure, run and test `miroir-collector` |
| [Sources](sources/) | One page per source, below |

## Sources

One row per connector in `src/integrations/registry.py`; a collector source is a
module in `collector/miroir_collector/sources/`. Any number of connections can
use a connector. The connections configured in an installation (their names,
schedules, datasets and dashboards) are in its `miroir.json`, which the
interface edits: read them there, or with `miroir connections`.

| Source | Reads | Connector | Collector source | Raw tables | Packages |
| --- | --- | --- | --- | --- | --- |
| [ListenBrainz](sources/listenbrainz.md) | Every listen, from the API | [`listenbrainz`](../src/integrations/listenbrainz/) | | `listens` | `music`, `crossovers` |
| [GitHub](sources/github.md) | Repos, commits, pull requests, issues, comments, releases, stars, events, contributions, from the API | [`github`](../src/integrations/github/) | | `repos`, `commits`, `pull_requests`, … | `github` |
| [Bike Share](sources/bikeshare.md) | Every rental, from the mobile app's API | [`bikeshare`](../src/integrations/bikeshare/) | | `trips` | `biking`, `crossovers` |
| [Apple Health](sources/apple-health.md) | Health Auto Export JSON files | [`apple_health`](../src/integrations/apple_health/) | | `metrics`, `workouts`, `state_of_mind`, `medications`, `other_records`, `other_collections` | `health`, `crossovers` |
| [Browser history](sources/browser-history.md) | Zen or Firefox history: collector batches or a Places snapshot | [`browser_history`](../src/integrations/browser_history/) | `zen_history` | `zen_*` | `browser`, `timeline` |
| [ScreenMemory](sources/screenmemory.md) | Screen text, front app, screenshots as media | [`screenmemory`](../src/integrations/screenmemory/) | `screenmemory` | `frames`, `app_usage` | `timeline` |
| [Apple Messages](sources/apple-messages.md) | Messages, chats, contacts, attachments as media | [`apple_messages`](../src/integrations/apple_messages/) | `apple_messages` | `messages`, `chats`, `handles`, `attachments`, … | `messages` |

Each source's handbook page says what it is and why it is kept. No connector
reads location history.

A source page covers: status, where the data comes from, extraction (resources,
keys, cursors) or what the collector sends and withholds, tables, models and
dashboards, credentials and settings, history, quirks.

### Adding a source

1. A connector package in `src/integrations/<name>/`, listed in `registry.py`:
   [Writing an integration](miroir/integrations.md). A device-only source also
   gets a collector source ([the collector](../collector/README.md#adding-a-source)).
2. dbt: the raw tables in `dbt/models/staging/_sources.yml`, `stg_<source>__*`
   views, models in `dbt/models/<group>/`, and a package in
   `src/analyses/catalog.py`.
3. A page in `sources/` and a row above, and the handbook's system page for it.
4. In the installation: create the connection in the interface, then sync it once.

## Secrets and local configuration

- Every credential a task needs is a Proton Pass reference in the
  `Automation General` vault, mapped to an environment variable in
  [`fnox.toml`](../fnox.toml): source tokens, and operator copies of the
  cluster's Prefect, warehouse and Miroir credentials, kept until tasks read
  those sources directly. Connections hold their own credentials in Miroir's
  encrypted store ([Configuration](miroir/configuration.md#credentials)).
- `mise run pass:login` opens the repo's session (the macOS Keychain, or a pod's
  `PROTON_PASS_TOKEN`); it extends the dotfiles' global `proton-login` task. Each
  task gets only the keys its `secrets = [...]` grant lists.
- Shared non-secret settings are in [`mise.toml`](../mise.toml); machine
  overrides go in the gitignored `mise.local.toml`. The repo selects no mise
  environment, so a machine keeps its own layers.
- Inside the cluster, select the tracked [`fnox.cluster.toml`](../fnox.cluster.toml)
  profile in `mise.local.toml`. It only points `PREFECT_API_URL` at the in-cluster
  service; do not also set that variable under `[env]`, where it conflicts with
  the task grant.

  ```toml
  [secrets.fnox]
  profile = "cluster"
  ```
