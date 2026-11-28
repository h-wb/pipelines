# miroir: instructions for agents

The code of Miroir, the warehouse, and of `miroir-collector`, the program that feeds it from a device. Start with the [handbook](https://github.com/h-wb/handbook): its [Warehouse](https://github.com/h-wb/handbook/blob/main/Systems/Warehouse.md) and [Device collector](https://github.com/h-wb/handbook/blob/main/Systems/Device%20collector.md) pages say how this fits with the rest. What is deployed and how to operate it is in [home-ops](https://github.com/h-wb/home-ops/blob/main/kubernetes/apps/main/automation/miroir/README.md), not here.

## Private data stays out

This repo is public and general. Work on it with made-up data only.

- Never read or query the warehouse, Metabase, the job runner's API or a database of the installation, and never open real exports, captured batches or screenshots. A bug that shows only on real data is reproduced in the private workshop.
- Never write personal details, a real domain, addresses, names of people, counts of real rows, or secrets: in code, tests, fixtures, pages, commits or pull requests. Write `miroir.example` for the server's address. Secrets are fnox references to Proton Pass (`fnox.toml`), never values.
- Fixtures in `tests/` and `collector/tests/` are synthetic. Keep them so.

## Layout

- `src/`: the server. `control/` the product, `integrations/` one package per connector, `analyses/catalog.py` the analysis packages, `web/` and `cli/` the two transports, `adapters/` Prefect, `flows/` the flow entry points. Who may import whom: [docs/architecture.md](docs/architecture.md), enforced by `tests/test_boundaries.py`; a new folder under `src/` fails it until it gets a rule.
- `collector/`: its own package, standard library plus urllib3, pydantic and the Kingfisher scanner. It never imports `src/` and nothing in `src/` imports it. Its rules are in `collector/tests/test_boundaries.py`.
- `dbt/`: the models the analyses build. `examples/`: a synthetic configuration.
- Pages: [docs/README.md](docs/README.md) is the index. Design decisions: [docs/miroir/decisions.md](docs/miroir/decisions.md), cited by number from code and pages.

## Run, test, lint

```bash
mise install && mise exec -- uv sync --dev                # once per worktree (paseo.json does it)
mise run dev                                               # local server, own empty state under .miroir/dev/
uv run --locked python -m unittest discover -s tests       # the full suite, collector included
uv run --locked ruff check src tests scripts collector
uv run --locked ruff format --check src tests scripts collector
uv run --locked mypy --follow-imports=silent src
uv run --locked mypy --strict --config-file collector/pyproject.toml collector/miroir_collector
```

- unittest, not pytest. The Postgres and dbt tests need Postgres binaries: set `MIROIR_TEST_POSTGRES_BIN` (in a workshop pod, `~/.local/share/mise/installs/postgres/17.11/bin`; 18.1 is installed too). Without them those classes error in `setUpClass`. The timeline's JavaScript tests run when `node` is on `PATH`.
- CI (`.github/workflows/test.yml`) runs the same lint, format, types and suite on every pull request and on `main`. Run them before each commit.

## Rules that are easy to break

- Branch and pull request; never push to `main`. The title follows Conventional Commits: pull requests are squash merged and the title decides the collector's next version ([docs/releases.md](docs/releases.md)). Never tag or edit the collector's version by hand.
- A change that makes a page wrong fixes the page in the same pull request.
- A design choice is written down with the code. A change to an existing decision is an amendment under its number. A new decision gets a page of its own in `docs/decisions/`, named by its day, as the handbook's [Write a page](https://github.com/h-wb/handbook/blob/main/Resources/Write%20a%20page.md) says; `docs/miroir/decisions.md` takes no new number.
- Persistent identities do not move: source, resource and table names, primary keys, dlt state keys, generated dataset names. Changing one is a data migration ([integrations](docs/miroir/integrations.md#compatibility-and-tests)).
- A new metadata schema (`SCHEMA_VERSION` in `src/control/operations.py`) means server, worker and flow-run image are released together; say so in the pull request.
- Product errors raise `OperationError`, so the interface and the CLI show them; no raw HTTP error or traceback reaches a user.
- Templates hold no inline style or script: the content security policy refuses them and `tests/test_web_static.py` fails.
- Container commands call the absolute interpreter, `/app/.venv/bin/python -m src.cli`.
