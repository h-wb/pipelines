# Pipelines

Personal data warehouse: [dlt](https://dlthub.com/) pipelines that mirror the
services I use into Postgres, dbt models on top, Metabase dashboards, all
scheduled by Prefect in the homelab ([h-wb/home-ops](https://github.com/h-wb/home-ops)).

**Documentation: [`docs/`](docs/README.md)** — architecture, conventions, one page
per source ([ListenBrainz](docs/sources/listenbrainz.md), [GitHub](docs/sources/github.md),
[Apple Health](docs/sources/apple-health.md), [Bike Share](docs/sources/bikeshare.md),
[browser history](docs/sources/browser-history.md)) and an [operations runbook](docs/operations.md).

## Layout

```
src/sources/     one dlt source per service (extract only, raw records)
src/pipelines/   one Prefect @flow per source (+ transform.py: run_dbt)
dbt/             staging views + mart schemas (music, health, biking, github, browser, crossovers)
prefect.yaml     deployments: schedules, parameters, env
fnox.toml        secret references (Proton Pass vault "env-dlt")
mise.toml        tools, shared config, task secret grants; mise.local.toml: machine overrides
docs/            documentation
```

## Setup

```bash
mise install      # uv, fnox, pass-cli
mise exec -- uv sync --dev
mise run pass:login  # macOS Keychain, or the pod's PROTON_PASS_TOKEN
mise secrets ls     # inspect names and task grants without fetching values
```

Paseo clones this repo during pod bootstrap. New worktrees run
`mise install && mise exec -- uv sync --dev` automatically via `paseo.json`.
Run `mise run pass:login` once per host before using tasks that need secrets;
worktrees share the repo's scoped session. Setup does not fetch secrets or run pipelines.
There is no repo `prod` mise environment: a project `.miserc.toml` would replace
the machine's environment list and disable its pod/Paseo layers. Shared defaults
live in `mise.toml`; put machine-specific overrides under `[env]` in gitignored
`mise.local.toml`. If you have `mise.prod.local.toml`, rename it to `mise.local.toml`.
The data paths default to the Prefect worker's mounts; override them for local imports.

## Common tasks

```bash
mise run deploy                                   # register all deployments (task-scoped secrets)
mise run prefect -- deployment run load-github/load_github --watch
mise run run:listenbrainz                         # run a pipeline locally (see mise.toml for all)
mise run run:dbt                                  # build + test the dbt models
uv run ruff check src/
```

Secrets are never in files here: add a field to the Proton Pass `env-dlt` vault and a
reference in `fnox.toml`. Details in [docs/README.md](docs/README.md#secrets-and-config).

Tasks receive only their declared fnox secrets; they are not exported to the shell.
For an ad-hoc command, use `mise x --secrets KEY -- <command>`.
