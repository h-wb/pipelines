---
area: "[[Homelab]]"
categories:
  - "[[Decisions]]"
summary: "The Prefect client locked in uv.lock is the contract with the Prefect server: the adapter refuses a server older than its minor or on another major, and CI runs the adapter's tests against a server of the locked version and of the latest of its major."
topics:
  - "[[Warehouse]]"
created: 2026-11-27T19:00
---

# The locked Prefect client is the contract with the Prefect server

Status: accepted, 2026-11-27. Refines [decision 013](../miroir/decisions.md#013--prefect-is-the-orchestrator-the-workspace-only-reconciles-with-it),
[029](../miroir/decisions.md#029--the-prefect-adapter-describes-deployments-in-prefects-own-attributes)
and the Prefect range of [004](../miroir/decisions.md#004--lock-application-dependencies-validate-outside-the-worker).

## Why

The adapter (`src/adapters/prefect.py`) already speaks only Prefect's SDK:
`Flow.to_deployment(...).apply()`, `run_deployment(...)` and the sync client.
Writing our own client for Prefect's HTTP API would make it looser, not tighter,
and undo 029. What was not pinned is the other side. The image locks the client
(`prefect>=3.4,<4` in `pyproject.toml`, the exact version in `uv.lock`), while the
installation upgrades the Prefect server on its own. Two gaps followed:

1. `tests/test_prefect.py` runs on `prefect_test_harness`, a server built from
   the client's own package. It proves only that a client works with a server of
   the same version.
2. Prefect itself refuses only another major. A server older than the client
   gets a log warning, and the adapter's calls (schedules, concurrency limits,
   flow run states) can change meaning in between.

## What is decided

**The locked client is the contract.** A Prefect server is compatible when it
shares the client's major and is at least the client's minor. Patch releases
differ freely in both directions; Prefect keeps a newer server serving older
clients within a major.

**The adapter checks before its first call.** Each public method of
`PrefectOrchestrator` asks the server's version (`/admin/version`) once per
process and API URL. An incompatible server is an `OperationError` that names
both versions and the version to upgrade to; nothing is planned, applied or
submitted. It is not `OrchestratorUnavailable`: retrying does not help until
someone upgrades. An unreachable or refusing server stays
`OrchestratorUnavailable`, as before.

**CI tests the adapter against a server of another process and version.** The
*Prefect contract* workflow (`.github/workflows/prefect-contract.yml`) starts
`prefect server start` with `scripts/prefect_contract.py` and runs
`tests/test_prefect.py` against it, through `MIROIR_TEST_PREFECT_API_URL`:

- `locked`: the locked version, from the project's own environment, so a
  dependency release cannot fail it falsely;
- `latest`: the latest release of the locked major, in an environment of its own.

It runs on pull requests that touch the adapter, the flows, the tests or the
lock, on `main`, and every Monday, so a Prefect release that breaks the adapter
shows within a week, before the installation upgrades to it. Locally:
`uv run --locked python -m scripts.prefect_contract latest` (or a version).

**Upgrade the server first.** A Prefect bump in `uv.lock` to a new minor needs a
server at that minor or later, or the adapter refuses it. So the server is
upgraded first: the `latest` job has already tested it against the image in
use. Then the lock follows, in a pull request whose `locked` job tests it. The
installation's side of this is in home-ops.

## Not done

- No version shown in the interface yet: the refusal carries both versions,
  which is where they matter.
- An older pinned server (for example `3.8.8`) can fail to start when `uv`
  installs it with dependencies newer than that release. The matrix tests only
  `locked` and `latest`, which do not hit this.
