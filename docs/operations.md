# Operations

All commands run from the repo root; `mise run prefect -- …` wraps the Prefect
CLI with the prod secrets from fnox.

## Everyday

Sources run as Miroir connections: sync, import, rebuild and inspect them in the
Miroir interface or its API/CLI ([Miroir](miroir/README.md)); `refresh` closes
runs that ended without recording an outcome.

```bash
mise run miroir:deploy                                    # apply Miroir deployments from miroir.json
mise run deploy                                           # (re)register run_dbt from prefect.yaml
mise run prefect -- deployment ls
mise run prefect -- flow-run ls --flow-name sync-connection --limit 5
mise run prefect -- flow-run logs <flow-run-id>
```

## dbt

Every Miroir load builds each analysis linked to its connection, into that
analysis's own schemas; a build failure leaves the load succeeded and the analysis
stale. `run_dbt` (nightly, legacy pool) builds the original mart schemas the
dashboards read, or a subset with `-p select=source:github+`.

Builds are serialised by the global concurrency limit `dbt`. It lives in the
Prefect server, not in `prefect.yaml`; recreate it after a server rebuild:

```bash
mise run prefect -- gcl create dbt --limit 1
```

## Full refresh

Miroir syncs have no full refresh. Never drop raw tables: dbt staging views depend
on them and Postgres refuses the `DROP TABLE`.

## Failure modes seen so far

| Symptom | Cause | Fix |
|---|---|---|
| Runs `Crashed` with `No module named prefect.runner._workspace_supervisor` / `workspace.json` missing | a repo installed a different Prefect into the shared worker | `kubectl -n automation rollout restart deploy/prefect-worker`; keep `prefect` a range in every deployed repo |
| `JSONDecodeError: Expecting value` mid-extract (ListenBrainz) | ListenBrainz served its HTML "Verifying your browser" page to our IP after heavy traffic | wait for `curl …/listens?count=1` to return `application/json`; the paginator already paces pages |
| `DependentObjectsStillExist: cannot drop table` | a refresh tried to drop a table dbt views depend on | use `drop_data` refreshes; then clear the pending package (next row) |
| Every run "load step did not start" / loads nothing | a failed load left a pending package in the worker's local pipeline dir | delete `/var/dlt/pipelines/<pipeline>` on the worker; dlt restores state from the warehouse on the next run |
| 403 from a public `*.<domain>` URL inside the cluster | Cloudflare blocks non-browser user agents | use the in-cluster service (with the public `Host` header if the app checks it) |

## One-off loads into the warehouse

The warehouse is only reachable in-cluster. Load files through a Miroir
connection (upload or receiving URL); flow runs have the credentials.

## Checks

```bash
# worker's Prefect must equal the image tag
kubectl -n automation exec deploy/prefect-worker -- python -c "import prefect;print(prefect.__version__)"
# what the worker sees in the shared data folder
kubectl -n automation exec deploy/prefect-worker -- ls -la /root/.prefect/data
```
