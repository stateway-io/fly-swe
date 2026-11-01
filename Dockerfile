FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.11 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY src/ ./src/
COPY prompts/ ./prompts/
# README.md and LICENSE are required at build time: pyproject.toml declares
# them as the package's `readme` and `license-files`, and `uv sync --frozen`
# (below) fails without them.
COPY README.md LICENSE ./
# Only the template ships in the image. The real /app/config.yaml is written at
# deploy time from the CONFIG_YAML Fly secret (see fly.toml [[files]]); with no
# config file present, Settings falls back to env vars + code defaults.
COPY config.yaml.example ./
COPY Procfile ./

RUN uv sync --frozen

RUN useradd -m -u 1000 agent && chown -R agent:agent /app
USER agent

EXPOSE 8080
CMD ["uv", "run", "honcho", "start"]
