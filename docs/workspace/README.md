# Workspace development

This is the implementation record for the personal data workspace. The current
slice is a read-only CLI and validated reference integrations. Production flows
continue to use their existing entry points and deployment configuration.

- [Roadmap](../product-plan.md)
- [Baseline and migration constraints](baseline.md)
- [Design decisions](decisions.md)
- [Implementation log and next steps](implementation-log.md)
- [Synthetic example configuration](../../examples/workspace.json)

## Try the first slice

From the repo root:

```bash
uv run --locked pipelines-workspace integrations
uv run --locked pipelines-workspace --config examples/workspace.json validate
uv run --locked pipelines-workspace --config examples/workspace.json connections
```

Output is JSON. Exit status is 0 for success and 2 for an invalid/unreadable
configuration. The default path is `workspace.json` in the current directory;
that root-level local file is gitignored. Pass `--config` before the subcommand.
No command fetches credentials, connects to services, starts a pipeline, or writes
configuration. The Python module entry point is `python -m src.cli`.

`integrations` lists connector metadata and settings schemas. Capabilities describe
underlying sources, not available execution commands: there is no workspace `sync`,
`import`, or `deploy` command yet. Apple Health currently advertises the existing
`directory_import`, not explicit artifact replay or native Apple Health XML support.

Configuration version 1 currently contains a workspace ID and connections. Each
connection has an integration, validated settings, credential references, and an
optional explicit pipeline/dataset binding. Credentials are objects with `provider`
(`env` or `fnox`) and a `key`; the corresponding resolver is future work. Inline
credential fields and unknown settings are rejected. The configuration's content
revision is a hash of normalized settings/references/bindings, not a deployed revision.

Generated names distinguish accounts and workspaces. Existing connections can be
mapped explicitly, for example:

```json
"binding": {
  "pipeline_name": "listenbrainz",
  "dataset_name": "listenbrainz_data"
}
```

This only describes a binding. It does not adopt, reset, or modify any live state.
The validator rejects duplicate connection IDs, pipeline names, and dataset names
within a workspace. The first design assumes one warehouse per workspace.

Schedules, source selection, destinations, machine-local overlays, stored immutable
revisions, secret resolution, and execution are deliberately absent from this
initial schema. Unsupported fields fail validation rather than being ignored.
Add each through a documented schema change when its operation is implemented.

## Validate in an isolated environment

Use a task-specific virtualenv so this does not change a shared worker environment:

```bash
export UV_PROJECT_ENVIRONMENT="$(mktemp -d /tmp/workspace-validation.XXXXXX)"
uv sync --locked --python 3.12 --no-dev --group test
RUNTIME__DLTHUB_TELEMETRY=false "$UV_PROJECT_ENVIRONMENT/bin/python" -m unittest discover -v
"$UV_PROJECT_ENVIRONMENT/bin/pipelines-workspace" --config examples/workspace.json validate
```

The test group adds DuckDB for synthetic source compatibility checks. HTTP calls
from the API source are intercepted; account tokens are fake and destinations are
temporary files. The checks exercise real dlt extraction/loading, independent
account namespaces, repeat imports, and restoration of committed cursor state into
a fresh local working directory. They do not prove Postgres-specific behavior,
Prefect worker execution, artifact replay, or crash recovery of pending packages.

For lint and type checks after `uv sync --locked --dev`:

```bash
uv run --locked ruff check src/core src/integrations src/cli tests
uv run --locked ruff format --check src/core src/integrations src/cli tests
uv run --locked mypy --follow-imports=silent src/core src/integrations src/cli
```

## How to continue this PR

For every slice, append a dated entry to the implementation log describing the
problem, changed behavior, validation commands/results, limitations, and next step.
Record architectural choices in the numbered decision log; supersede old decisions
explicitly rather than silently erasing their rationale. Update roadmap status
when its completion criteria are actually met. Keep production cutover a distinct,
reviewable step with its own baseline comparison and recovery procedure.
