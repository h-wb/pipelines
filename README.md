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
dbt/             staging views + mart schemas (music, health, biking, github, crossovers)
prefect.yaml     deployments: schedules, parameters, env
fnox.toml        secret references (Proton Pass vault "dlt")
mise.toml        tools, local config, tasks; mise.prod.toml: prod config
docs/            documentation
```

## Setup

```bash
mise install      # uv, fnox, pass-cli
uv sync --dev
fnox check        # every secret in fnox.toml resolves (first use: pass-cli login)
```

## Common tasks

```bash
mise run deploy                                   # register all deployments (prod env + secrets)
mise run prefect -- deployment run load-github/load_github --watch
mise run run:listenbrainz                         # run a pipeline locally (see mise.toml for all)
mise run run:dbt                                  # build + test the dbt models
uv run ruff check src/
```

Secrets are never in files here: add a field to the Proton Pass `dlt` vault and a
reference in `fnox.toml`. Details in [docs/README.md](docs/README.md#secrets-and-config).
