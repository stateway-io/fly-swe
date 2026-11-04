from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from issue_agent.config import get_settings
from issue_agent.logging import get_logger, setup_logging
from issue_agent.observability.prometheus_metrics import (
    init_metrics,
    start_metrics_server,
)
from issue_agent.providers.registry import ProviderRegistry
from issue_agent.queue.store import SQLiteStore
from issue_agent.webhook.function_calling import FunctionCallingCheck
from issue_agent.webhook.health import router as health_router
from issue_agent.webhook.routes import router as webhook_router

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level, settings.agent_name)

    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    db_path = str(data_dir / "issue_agent.db")
    store = SQLiteStore(db_path)
    store.init()

    app.state.store = store
    app.state.registry = ProviderRegistry.from_settings(settings)

    # Advisory only, and it needs LiteLLM — whose import alone takes seconds.
    # Running it here would hold uvicorn's port closed (and every webhook the
    # Fly proxy is queueing during a cold start) until it finished, so it runs
    # in the background and /readyz reports "pending" meanwhile.
    app.state.function_calling = FunctionCallingCheck(settings)
    app.state.function_calling.start()

    init_metrics(settings)

    try:
        start_metrics_server(settings.metrics_port)
        logger.info("metrics_server_started", port=settings.metrics_port)
    except OSError:
        logger.warning("metrics_server_port_in_use", port=settings.metrics_port)

    logger.info("app_started", db_path=db_path)

    yield

    store.close()
    logger.info("app_stopped")


def _app_title() -> str:
    # Best-effort at import time: get_settings() is lru_cache'd, so this is
    # cheap once secrets are present. In constrained environments where
    # required secrets (LLM_API_KEY, a provider instance) aren't set yet
    # (e.g. some test contexts), fall back to a generic title rather than
    # crashing module import — lifespan() re-validates settings for real at
    # startup and fails loudly there if config is actually missing.
    try:
        return f"{get_settings().agent_name} Service"
    except Exception:
        return "Issue Agent Service"


app = FastAPI(title=_app_title(), lifespan=lifespan)
app.include_router(webhook_router)
app.include_router(health_router)
