# DLT Pipelines

A collection of data pipelines using [dlt](https://dlthub.com/) for various data sources.


## Project Structure

```
├── src/
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── listenbrainz.py      # ListenBrainz data source
│   │   ├── arc_timeline.py      # Arc Timeline data source
│   │   └── bikeshare.py         # Bike Share Toronto data source
│   └── pipelines/
│       ├── __init__.py
│       ├── listenbrainz.py      # ListenBrainz pipeline
│       ├── arc_timeline.py      # Arc Timeline pipeline
│       └── bikeshare.py         # Bike Share Toronto pipeline
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

#### Bike Share Toronto Pipeline
Extract trip history from Bike Share Toronto mobile API.

```bash
uv run python src/pipelines/bikeshare.py
```

### Deploy with Prefect

#### Prerequisites
1. Set up required environment variables (locally or on remote server):
   ```bash
   # ListenBrainz
   export LISTENBRAINZ__USERNAME="your_username"
   export LISTENBRAINZ__ACCESS_TOKEN="your_token"
   export LISTENBRAINZ__START_DATE="2025-10-05"

   # Arc Timeline
   export ARC_TIMELINE__APPLE_ID="your_apple_id@icloud.com"
   export ARC_TIMELINE__PASSWORD="your_password"

   # Bike Share Toronto (Mobile API)
   export BIKESHARE__MEMBER_ID="your_member_id"
   export BIKESHARE__AUTHORIZATION_TOKEN="your_auth_token"

   # DuckDB destination
   export DESTINATION__DUCKDB__DESTINATION_NAME="/path/to/db"
   ```

   Or set them in your mise environment file (e.g., `.mise.prod.toml`):
   ```toml
   [env]
   # ListenBrainz
   LISTENBRAINZ__USERNAME = "your_username"
   LISTENBRAINZ__ACCESS_TOKEN = "your_token"
   LISTENBRAINZ__START_DATE = "2025-10-05"

   # Arc Timeline
   ARC_TIMELINE__APPLE_ID = "your_apple_id@icloud.com"
   ARC_TIMELINE__PASSWORD = "your_password"

   # Bike Share Toronto (Mobile API)
   BIKESHARE__MEMBER_ID = "your_member_id"
   BIKESHARE__AUTHORIZATION_TOKEN = "your_auth_token"

   # DuckDB destination
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
uv run python src/pipelines/bikeshare.py
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
