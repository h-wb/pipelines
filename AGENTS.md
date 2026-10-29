# AGENTS.md — working on this repo (Miroir)

Miroir is a self-hosted personal-data mirror: connections to personal sources
(ListenBrainz, Apple Health exports, GitHub, …) are created in a web interface,
executed as Prefect flow runs, loaded with dlt into a Postgres warehouse, and
modelled with dbt for Metabase. The product layer (config, credentials, status,
artifacts) is thin and home-grown; everything beneath it is commodity
infrastructure. Long-term direction: open source + a paid hosted tier; the
orchestrator adapter (below) is the seam that keeps the hosted option open.

**Read first:** `docs/miroir/decisions.md` (numbered, chronological — the
ground truth for *why*), `docs/miroir/README.md` (how everything is used),
`docs/miroir/implementation-log.md` (what was built, per session, with
validation notes). Older entries say "workspace" — the product was renamed to
Miroir (decision 014); history keeps its own words.

## Repo map

- `src/core/` — the product. `config.py` (validation, revisions,
  `metadata_url()`), `operations.py` (MetadataStore: operations, attempts,
  encrypted secrets, artifact registry; Postgres schema `miroir_meta`),
  `runner.py` (register → lock → load → record → model), `service.py`
  (everything both transports call), `secrets.py` (Fernet built-in store,
  env/fnox backends), `artifacts.py` (content-addressed receipt).
- `src/web/` — FastAPI app + templates. Token auth (`MIROIR_API_TOKEN`).
  `connections.py` owns guided setup and connection settings; activity/ingestion
  routes stay in `app.py`. Integration delivery metadata drives the forms.
- `src/cli/` — `pipelines-mirroir`, one CLI over the same service.
- `src/adapters/prefect.py` — **the orchestrator seam** (decision 013): three
  verbs — `plan`/`apply` (deployments from config, drift reporting), `submit`
  (request → flow run, idempotent on the operation ID), `observe` (close
  unverified attempts from flow-run state). Any future hosted backend
  implements the same three verbs.
- `src/pipelines/miroir.py` — the Prefect flow `sync_connection` (one
  deployment per connection), and `transform.py` (dbt build).
- `src/integrations/`, `src/sources/` — connector registry, checks, dlt
  sources. Integrations declare settings schemas and credential fields.
- `dbt/` — models (staging + reporting), profiles read
  `DESTINATION__POSTGRES__CREDENTIALS__*` env.
- `tests/` — unittest; no pytest. 99 tests, ~95s, include four against a real
  temporary Prefect API (`prefect_test_harness`), CLI subprocess tests, and three
  disposable Postgres/dbt scenarios (set `MIROIR_TEST_POSTGRES_BIN` to the bin
  directory of an installed Postgres, or put `initdb`/`pg_ctl` on PATH).

## Commands

```bash
uv run --locked python -m unittest discover -s tests          # the suite
uv run --locked ruff check --fix <paths> && uv run --locked ruff format <paths>
uv run --locked mypy --follow-imports=silent src/...          # workspace slice only
mise run miroir:deploy    # deployments apply (needs fnox secrets, see below)
mise run prefect -- <cmd> # prefect CLI with cluster credentials
```

Lint/mypy the *slice* (`src/core src/adapters src/integrations src/cli src/web
src/pipelines/miroir.py tests`) — legacy `src/pipelines/*.py` have pre-existing
findings. Secrets for mise tasks come from ProtonPass via fnox
(`mise run pass:login`); grants are per task in `mise.toml`/`fnox.toml`.

## The deployed system (home-ops cluster, `automation` namespace)

- **Image**: `ghcr.io/h-wb/pipelines`, built by `.github/workflows/image.yml`
  on push to main. Tags: semver, `main`, `latest`, and `sha-<40hex>` — **always
  reference `sha-` tags**; the cluster's spegel mirror serves stale mutable
  tags (a `latest` once ran pre-rename code).
- **home-ops repo** (`~/Repositories/home-ops`, branch `main`, Flux GitOps,
  push with `--no-verify` from this pod — the pre-push hook fails on sandbox
  registry auth for unrelated apps):
  - `automation/miroir/` — the CNPG cluster `postgres-miroir` (cnpg/initdb
    component) and the server HelmRelease (`pipelines-mirroir serve`, route
    `miroir.<SECRET_DOMAIN>` behind envoy-internal).
  - `automation/prefect/app/` — the Prefect server + legacy process worker
    (`local-pool`) and the miroir Kubernetes worker (`miroir-k8s`), the job
    template ConfigMap (NFS mount at `/miroir/data`, credentials via
    secretKeyRef), the `miroir` ExternalSecret (→ `miroir-secret`), RBAC.
- **State**: `miroir.json` + artifacts live on the NFS export
  (`/mnt/tank/hwe/Documents/data/miroir/`); flow-run Jobs mount it at
  `/miroir/data`. Metadata DB: schema `miroir_meta` in postgres-miroir
  (credentials from the CNPG `postgres-miroir-app` secret; the URL composes
  from `MIROIR_METADATA__*` — pin the psycopg2 dialect, see decisions).
- **Secrets**: `miroir-secret` (MIROIR_SECRET_KEY, MIROIR_API_TOKEN) from the
  Bitwarden item `miroir`; warehouse credentials from `postgres-metabase-app`.
  The workspace master key is the ONLY installation secret not derived in-cluster.

## Release and deploy cycle

1. Commit to `main` and push (pipelines repo). CI builds the image (≈2 min).
2. Set the new `sha-<full-commit>` in the live miroir.json `deployment.image`
   and Apply deployments; then pin it in home-ops (server + worker HelmRelease
   images) and push home-ops `main`. Execution settings now live in the config;
   the server no longer carries pool/image/job-variable fallback pins.
3. `flux -n flux-system reconcile source git flux-system`, then
   `flux -n automation reconcile kustomization <app> --with-source` (the
   kubeconfig: `kubernetes/clusters/main/kubeconfig` in home-ops; `kubectl`
   works read-only, `flux` reconcile and pod deletion are allowed).
4. Verify server/worker readiness, a flow run using the same image, and an empty
   deployments plan. Schema 4 was deployed with decision 017; schema 3 runtimes
   cannot run against it.

## Working conventions

- **Decisions first**: design questions get an entry in `decisions.md` before
  or with the code; every session appends to `implementation-log.md` (what,
  why, validation). Commits follow the log.
- **Tests are the check**; the Prefect tests run against a real ephemeral API.
  New code runs lint + format + mypy + the suite before commit.
- **No secret values in this repo** — only ProtonPass/fnox references.
- **Readable errors**: product errors raise `OperationError` so both the web UI
  and CLI render them properly; never let raw HTTP/traceback errors escape.
- Container commands invoke the absolute interpreter
  (`/app/.venv/bin/python -m src.cli` / `-m prefect`) — a node once refused
  PATH lookups of console scripts despite a correct image.

## Known remaining work (2026-10-29)

- Add a schedule to the ListenBrainz connection (cron in config → Apply
  deployments). First sync already succeeded end to end.
- Apple Health n8n cutover (decision 009): point Health Auto Export at
  `POST /api/connections/health/receive` with a connection-specific delivery token
  (decision 017 is deployed). Folder-free setup and token management are live;
  real phone delivery is verified. Metadata schema 4 requires updating the server,
  worker, and flow-run image together.
- Legacy cutover: map the connection binding to `listenbrainz_data`
  explicitly when ready, disable the legacy schedule; delete the stale
  `workspace-k8s` Prefect pool; eventually retire `local-pool`.
- Cloud blockers (decision-adjacent, from the OSS+hosted analysis):
  per-tenant key separation, artifact storage abstraction (NFS paths are
  absolute on purpose today — they must resolve identically everywhere),
  multi-tenant metadata partitioning, accounts/OIDC.
- Cosmetic: generated dataset bindings still use the `ws_` prefix (pre-rename
  abbreviation); free to change while no data depends on it.
