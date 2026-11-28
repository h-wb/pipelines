---
area: "[[Homelab]]"
categories:
  - "[[Resources]]"
reviewed: 2026-11-24
summary: What this repo holds, how to set up a checkout, and where its pages are.
topics:
  - "[[Warehouse]]"
  - "[[Device collector]]"
created: 2025-10-06T20:46
---

# miroir

Miroir, a personal data warehouse: connections mirror the services and devices I
use into Postgres with [dlt](https://dlthub.com/), analyses build dbt models on
top for Metabase and a timeline, and Prefect runs every load. The
`miroir-collector` program, in `collector/`, feeds it from a device. How it fits
with the rest: the handbook's
[Warehouse](https://github.com/h-wb/handbook/blob/main/Systems/Warehouse.md).
It is deployed from
[home-ops](https://github.com/h-wb/home-ops/blob/main/kubernetes/apps/main/automation/miroir/README.md).

Documentation: [docs/](docs/README.md). Rules for agents: [AGENTS.md](AGENTS.md).

## Layout

```
src/core/          Miroir: configuration, metadata store, runner, services, media, timeline
src/integrations/  one connector package per source
src/analyses/      the catalog of analysis packages
src/web, src/cli   the interface, HTTP API and CLI over the same services
src/adapters/      Prefect
src/flows/         the flow entry points Prefect runs
collector/         miroir-collector, its own package
dbt/               staging views and one model group per package
docs/              documentation
```

## Set up a checkout

```bash
mise install                # uv, fnox, pass-cli
mise exec -- uv sync --dev
mise run dev                # the interface on http://127.0.0.1:8765, local state only
```

Paseo runs the first two in a new worktree (`paseo.json`) and offers `dev` and
`infra` as service scripts. `mise run dev` needs no secret.

Tasks that reach the installation (`dev:infra`, `miroir:deploy`,
`miroir:refresh`, `prefect`) need a Proton Pass session: `mise run pass:login`
once per host. Each task receives only the fnox secrets its `secrets = [...]`
grant lists; nothing enters the shell. `mise secrets ls` shows names and grants
without fetching values; an ad-hoc command uses `mise x --secrets KEY -- <command>`.
Shared defaults are in `mise.toml`; machine overrides go in the gitignored
`mise.local.toml`. More: [development](docs/miroir/README.md#develop-it).
