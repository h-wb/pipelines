# syntax=docker/dockerfile:1

# One image for the whole runtime side (decision 012): flow runs in Kubernetes
# Jobs, the Kubernetes worker, and (later) the workspace server. Nothing is
# installed at run time; uv.lock is the source of truth for every dependency,
# and the image digest identifies the exact code a flow run executed.

# --- dependency + project build ---
# Pinned to the build platform: the venv is copied into the runtime stage, so
# each target platform gets a native build (no QEMU).
FROM --platform=$BUILDPLATFORM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1
# Dependencies first: the lockfile layer only rebuilds when it changes.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
# The project itself (src/ is the package, dbt/ is the transform project).
COPY src/ src/
COPY dbt/ dbt/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# --- runtime ---
FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    WORKSPACE_PREFECT_CONFIG_PATH=/workspace/data/workspace/workspace.json
# Set by CI to the commit the image was built from; recorded on every attempt.
ARG PIPELINES_CODE_REVISION=unknown
ENV PIPELINES_CODE_REVISION=${PIPELINES_CODE_REVISION}
CMD ["python", "-m", "src.cli"]
