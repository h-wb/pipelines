# DLT Pipelines

A collection of data pipelines using [dlt](https://dlthub.com/) for various data sources.


## Project Structure

```
├── src/
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── listenbrainz.py      # ListenBrainz data source
│   │   └── arc_timeline.py      # Arc Timeline data source
│   └── pipelines/
│       ├── __init__.py
│       ├── listenbrainz.py      # ListenBrainz pipeline
│       └── arc_timeline.py      # Arc Timeline pipeline
├── pyproject.toml               # Project configuration
├── .env.example                 # Environment variables template
└── README.md
```

## Setup

1. **Install dev dependencies** (using mise):
   ```bash
   mise install
   ```

2. **Create virtual environment and install dependencies**:
   ```bash
   uv sync
   ```

3. **Configure environment variables**:
   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

## Usage

### Available Pipelines

#### ListenBrainz Pipeline
Extract listening history from ListenBrainz.
```bash
uv run python src/pipelines/listenbrainz.py
```

#### Arc Timeline Pipeline
Extract Arc Timeline data from Arc Editor exports in iCloud Drive (`Arc Editor/Exports` folder).
```bash
uv run python src/pipelines/arc_timeline.py
```

### Deploy with Prefect

#### Prerequisites
1. Set up required environment variables (locally or on remote server):
   ```bash
   export LISTENBRAINZ__USERNAME="your_username"
   export LISTENBRAINZ__ACCESS_TOKEN="your_token"
   export LISTENBRAINZ__START_DATE="2025-10-05"
   export DESTINATION__DUCKDB__DESTINATION_NAME="/path/to/db"
   ```

   Or set them in your mise environment file (e.g., `.mise.prod.toml`):
   ```toml
   [env]
   LISTENBRAINZ__USERNAME = "your_username"
   LISTENBRAINZ__ACCESS_TOKEN = "your_token"
   LISTENBRAINZ__START_DATE = "2025-10-05"
   DESTINATION__DUCKDB__DESTINATION_NAME = "/path/to/db"
   ```

2. Make sure you have a Prefect work pool created:
   ```bash
   prefect work-pool create local-pool --type process
   ```

#### Deployment Steps

1. **Deploy to Prefect** (from your local machine or remote server):
   ```bash
   # Set the environment if using mise environments
   export MISE_ENV=prod  # or whatever environment you're deploying to

   # Deploy the flows
   prefect deploy --prefect-file prefect.yaml
   ```

2. **Start Prefect worker** (on your remote server ONLY):
   ```bash
   # On the remote server where you want flows to execute
   prefect worker start --pool "local-pool"
   ```

   ⚠️ **Important**: Only run the worker on the machine where you want the flows to execute.
   Do NOT run workers locally if you want flows to run on a remote server.

3. **Monitor deployments**:
   - View in Prefect UI or use:
   ```bash
   prefect deployment run load_listenbrainz/load_listenbrainz --watch
   ```

### Configuration

Each pipeline uses dlt's configuration system via environment variables. Configuration examples are in `.env.example`.


## Prefect Integration

The pipelines use environment variables for configuration in both local and Prefect deployments.

### Local Development
Run pipelines directly:
```bash
uv run python src/pipelines/listenbrainz.py
uv run python src/pipelines/arc_timeline.py
```

## Development

Install development dependencies:
```bash
uv sync --dev
```

Run linting:
```bash
uv run ruff check src/
uv run black src/
```

Run type checking:
```bash
uv run mypy src/
```
