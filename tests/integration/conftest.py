from __future__ import annotations

import pytest
from fastapi import FastAPI
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings, get_settings
from issue_agent.providers.registry import ProviderRegistry
from issue_agent.queue.store import SQLiteStore
from issue_agent.webhook.health import router as health_router
from issue_agent.webhook.routes import router as webhook_router
from tests.helpers.github import (
    DEFAULT_INSTALLATION_ID,
    TEST_SECRET,
    make_github_payload,
    sign_payload,
)
from tests.helpers.settings import github_providers

# Re-exported: the integration tests import these from here.
__all__ = ["DEFAULT_INSTALLATION_ID", "TEST_SECRET", "make_github_payload", "sign_payload"]


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


def _make_settings(**overrides: dict) -> Settings:
    defaults = dict(
        PROVIDERS=github_providers(),
        LLM_API_KEY="sk-test-anthropic",
        REPO_ROUTES=[
            {
                "issues": "stateway-io/stateway-issues",
                "target": "stateway-io/stateway-agent-fixtures",
            }
        ],
    )
    defaults.update(overrides)
    return SettingsNoEnv(**defaults)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
def store() -> SQLiteStore:
    s = SQLiteStore(":memory:")
    s.init()
    return s


@pytest.fixture
def worker_settings(tmp_path: object) -> Settings:
    return _make_settings(DATA_DIR=str(tmp_path))


@pytest.fixture
def app(store: SQLiteStore, monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> FastAPI:
    # A tmp data dir so the webhook handler's audit writes (issue lifecycle)
    # never land in the repo's ./data during tests.
    settings = _make_settings(DATA_DIR=str(tmp_path / "webhook-data"))
    monkeypatch.setattr("issue_agent.webhook.routes.get_settings", lambda: settings)
    _app = FastAPI()
    _app.state.store = store
    _app.state.registry = ProviderRegistry.from_settings(settings)
    _app.include_router(webhook_router)
    _app.include_router(health_router)
    return _app


@pytest.fixture
def app_with_bot_login(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> FastAPI:
    """App fixture with an explicit providers.github.bot_login configured."""
    settings = _make_settings(
        DATA_DIR=str(tmp_path / "webhook-data"),
        AGENT_NAME="issue-agent",
        PROVIDERS=github_providers(bot_login="stateway-issue-agent[bot]"),
    )
    monkeypatch.setattr("issue_agent.webhook.routes.get_settings", lambda: settings)
    _app = FastAPI()
    _app.state.store = store
    _app.state.registry = ProviderRegistry.from_settings(settings)
    _app.include_router(webhook_router)
    _app.include_router(health_router)
    return _app
