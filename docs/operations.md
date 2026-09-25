# Operations

All commands run from the repo root; `mise run prefect -- …` wraps the Prefect
CLI with the prod secrets from fnox.

## Everyday

```bash
mise run deploy                                           # (re)register every deployment from prefect.yaml
mise run prefect -- deployment ls
mise run prefect -- deployment run load-github/load_github --watch
mise run prefect -- deployment run load-listenbrainz/load_listenbrainz -p full_refresh=true --watch
mise run prefect -- flow-run ls --flow-name load-github --limit 5
mise run prefect -- flow-run logs <flow-run-id>
```

A deployment only accepts parameters that exist in its registered schema, so
after adding a flow parameter run `mise run deploy` before passing it.

## Full refresh

`full_refresh=true` truncates the source's tables and resets its incremental
state (`refresh="drop_data"`). It never drops tables: dbt staging views depend
on them and Postgres refuses the `DROP TABLE` (the load then fails and leaves a
pending package, see below).

## Failure modes seen so far

| Symptom | Cause | Fix |
|---|---|---|
| Runs `Crashed` with `No module named prefect.runner._workspace_supervisor` / `workspace.json` missing | a repo installed a different Prefect into the shared worker | `kubectl -n automation rollout restart deploy/prefect-worker`; keep `prefect` a range in every deployed repo |
| `JSONDecodeError: Expecting value` mid-extract (ListenBrainz) | ListenBrainz served its HTML "Verifying your browser" page to our IP after heavy traffic | wait for `curl …/listens?count=1` to return `application/json`; the paginator already paces pages |
| `DependentObjectsStillExist: cannot drop table` | a refresh tried to drop a table dbt views depend on | use `drop_data` refreshes; then clear the pending package (next row) |
| Every run "load step did not start" / loads nothing | a failed load left a pending package in the worker's local pipeline dir | delete `/var/dlt/pipelines/<pipeline>` on the worker; dlt restores state from the warehouse on the next run |
| 403 from a public `*.<domain>` URL inside the cluster | Cloudflare blocks non-browser user agents | use the in-cluster service (with the public `Host` header if the app checks it) |

## One-off loads into the warehouse

The warehouse is only reachable in-cluster. Prefer running a deployment with
parameters (the worker has the credentials). For ad-hoc runs, copy the code or
data onto the worker (`kubectl -n automation exec -i deploy/prefect-worker -- tar -x …`)
and run there; remove copied data afterwards (exports contain private repos).

## Checks

```bash
# worker's Prefect must equal the image tag
kubectl -n automation exec deploy/prefect-worker -- python -c "import prefect;print(prefect.__version__)"
# what the worker sees in the shared data folder
kubectl -n automation exec deploy/prefect-worker -- ls -la /root/.prefect/data
```
