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
   ```

4. **OR - Set up dlt configuration**:
   ```bash
   # Create config files (if not already done)
   mkdir -p .dlt
   # Copy .env.example to .dlt/config.toml and .dlt/secrets.toml
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

### Run with Prefect

`prefect deploy --prefect-file prefect.yaml`

### Configuration

Each pipeline uses dlt's configuration system. Configuration examples are in `.env.example`.

#### ListenBrainz Configuration
Set these in your `.dlt/config.toml`:
```toml
[sources.listenbrainz]
username = "your_username"
```

And in `.dlt/secrets.toml`:
```toml
[sources.listenbrainz]
access_token = "your_api_token"
```

#### Arc Timeline Configuration
Set these in your `.dlt/config.toml`:
```toml
[sources.arc_timeline]
apple_id = "your_apple_id@icloud.com"
```

And in `.dlt/secrets.toml`:
```toml
[sources.arc_timeline]
password = "your_icloud_password"
```


## Prefect Integration

The pipelines support both local development (using `.dlt/` config files) and Prefect deployments (using Prefect Secret blocks).

### Local Development
Run pipelines directly using dlt config files:
```bash
uv run python src/pipelines/listenbrainz.py
uv run python src/pipelines/arc_timeline.py
```

### Prefect Deployments
1. **Set up Prefect Secret blocks** (one-time setup):
   ```bash
   uv run python setup_prefect_blocks.py
   ```

2. **Use Prefect flows**:
   ```python
   from src.pipelines.listenbrainz import load_listenbrainz_from_prefect_blocks
   from src.pipelines.arc_timeline import load_arc_timeline_from_prefect_blocks
   
   # Run flows that use Prefect blocks
   load_listenbrainz_from_prefect_blocks()
   load_arc_timeline_from_prefect_blocks()
   ```

3. **Or pass credentials directly**:
   ```python
   from src.pipelines.listenbrainz import load_listenbrainz
   
   load_listenbrainz(username="your_user", access_token="your_token")
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
