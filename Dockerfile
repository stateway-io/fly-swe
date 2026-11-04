FROM python:3.12-slim AS base

# build-essential stays on purpose: it is for the coding agent, not for this
# service's own dependencies (all ship wheels) — the target repos it works on
# may need a C toolchain (node-gyp, Python C extensions) to install and test.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.11 /uv /uvx /bin/

# Created before anything lands in /app, so every file below is owned by
# `agent` from the layer that writes it. A trailing `chown -R` would copy the
# whole virtualenv (~550 MB) into one more layer — the image would carry it
# twice, and a cold-starting Machine would have to pull both copies.
RUN useradd -m -u 1000 agent \
    && install -d -o agent -g agent /app

# UV_COMPILE_BYTECODE: ship .pyc files in the image. A Machine's root
#   filesystem resets to the image on every start, so .pyc written at runtime
#   never survive — without this, every boot recompiles every module it imports.
# UV_LINK_MODE=copy: the uv cache is a BuildKit cache mount, a different
#   filesystem from /app, so hardlinking can't work; say so instead of warning.
# PATH/VIRTUAL_ENV: the venv's executables directly, so the container starts
#   `honcho` itself instead of going through `uv run` (which re-checks the
#   environment on every start). The agent's shell still inherits the venv on
#   its PATH, as it did under `uv run`.
# PYTHONUNBUFFERED: log lines reach `fly logs` as they happen.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1

USER agent
WORKDIR /app

COPY --chown=agent:agent pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/home/agent/.cache/uv,uid=1000,gid=1000 \
    uv sync --frozen --no-install-project

COPY --chown=agent:agent src/ ./src/
COPY --chown=agent:agent prompts/ ./prompts/
# README.md and LICENSE are required at build time: pyproject.toml declares
# them as the package's `readme` and `license-files`, and `uv sync --frozen`
# (below) fails without them.
COPY --chown=agent:agent README.md LICENSE ./
# Only the template ships in the image. The real /app/config.yaml is written at
# deploy time from the CONFIG_YAML Fly secret (see fly.toml [[files]]); with no
# config file present, Settings falls back to env vars + code defaults.
COPY --chown=agent:agent config.yaml.example ./
COPY --chown=agent:agent Procfile ./

# The project is installed editable (it runs from /app/src), so
# UV_COMPILE_BYTECODE — which compiles site-packages — doesn't reach it:
# compile it here too.
RUN --mount=type=cache,target=/home/agent/.cache/uv,uid=1000,gid=1000 \
    uv sync --frozen \
    && python -m compileall -q src

EXPOSE 8080
CMD ["honcho", "start"]
