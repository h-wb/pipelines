# Implementation log

## 2026-10-28 — Planning and baseline

- Preserved the reviewed roadmap and its acquisition/import separation, staged
  delivery, state ownership, and migration constraints in `docs/product-plan.md`.
- Created `feat/personal-data-workspace` for incremental work in a draft PR.
- Read the existing reference flows/sources, dbt source bindings, deployment
  configuration, source pages, and operations runbook. Recorded repository facts
  and separately identified live worker image/mount observations in `baseline.md`.
- No production flow was triggered and no deployment configuration was changed.

## 2026-10-28 — Configuration, registry, CLI, and source adapters

Implemented:

- Strict versioned JSON workspace/connection configuration and named credential
  references. Unknown fields, invalid dates/types, duplicate JSON keys, and conflicting
  pipeline/dataset bindings fail validation.
- Stable generated bindings, explicit legacy mappings, and normalized revision hashes.
- Bundled ListenBrainz / Apple Health definitions and lazy adapters that pass settings
  and resolved credentials directly to the existing sources.
- Read-only `pipelines-workspace integrations|validate|connections` commands, JSON
  output, and safe diagnostics. Local configuration/input paths are gitignored.
- Committed dependency locking, explicit Pydantic dependency, and a DuckDB test group.

Validation:

- Installed from the lockfile into a separate Python 3.12 environment.
- `RUNTIME__DLTHUB_TELEMETRY=false <isolated-python> -m unittest discover -v`:
  13 tests passed. These cover malformed/secret-bearing configuration, binding
  collisions, CLI runtime/network isolation, real dlt source loading, repeat imports,
  independent API accounts, and restoring committed state into a fresh local directory.
- Source tests used synthetic files and intercepted HTTP, fake tokens, and a temporary
  DuckDB database. The existing source and production flow files were not edited.
- Final checks after `uv sync --locked --dev` (repo `.venv`, Python 3.14.7, ruff 0.17.3,
  mypy 2.4.0): `ruff check` and `ruff format --check` clean on the new packages and
  tests; `mypy --follow-imports=silent src/core src/integrations src/cli` reports no
  issues (needed `explicit_package_bases`); the unittest suite passes there too.
- CLI smoke: `validate` on `examples/workspace.json` exits 0 with two connections;
  a missing config file exits 2 with a value-free diagnostic. `uv build --wheel`
  includes `src/core`, `src/integrations`, `src/cli`, and the `pipelines-workspace`
  entry point.

Milestone status:

| Milestone | Status |
| --- | --- |
| 0: baseline/runtime boundary | Reference baseline and local dependency/source proof done; actual work-pool, secret, storage, and Postgres proof pending |
| 1: contracts/registry | Initial reader, explicit catalog, source adapters, CLI, and contract tests implemented |
| 2: execution/status | Not implemented; no operation metadata, secret resolver, scheduler reconciliation, or execution command |
| 3: import inbox | Not implemented; current directory import is compatibility-tested, not an artifact registry/replay implementation |
| 4+: interface/migration/analyses | Not started |

Next slice:

1. Establish the runnable Prefect isolation path and worker credential/storage access.
2. Define operation/attempt records and the worker entry point shared by schedules,
   CLI requests, and direct/n8n triggers.
3. Add a supported API sync in an isolated destination with truthful load/build status.
4. Then implement explicit artifact registration/parsing, replacing path/mtime assumptions
   for new manual imports while keeping the legacy scanner available during migration.
