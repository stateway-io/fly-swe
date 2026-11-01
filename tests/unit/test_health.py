from __future__ import annotations

from importlib.metadata import version as pkg_version

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings, get_settings
from issue_agent.providers.base import ProviderHealth
from issue_agent.providers.github.plugin import GitHubProviderPlugin
from issue_agent.providers.registry import ProviderRegistry
from issue_agent.queue.store import SQLiteStore
from issue_agent.webhook.health import router as health_router
from tests.fakes.fake_provider import FakeProvider
from tests.helpers.settings import github_providers

_INSTALLED_VERSION = pkg_version("fly-swe")


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


def _make_settings(**overrides) -> Settings:
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
def app(store: SQLiteStore) -> FastAPI:
    app = FastAPI()
    app.state.store = store
    app.include_router(health_router)
    return app


def _configure(monkeypatch: pytest.MonkeyPatch, app: FastAPI, settings: Settings) -> None:
    """What the lifespan does at startup: settings, and the provider
    registry built from them."""
    monkeypatch.setattr("issue_agent.webhook.health.get_settings", lambda: settings)
    app.state.registry = ProviderRegistry.from_settings(settings)


def _client(app: FastAPI):
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ── /healthz ────────────────────────────────────────────────────


class TestHealthz:
    @pytest.mark.asyncio
    async def test_returns_200(self, monkeypatch: pytest.MonkeyPatch, app: FastAPI) -> None:
        _configure(
            monkeypatch,
            app,
            _make_settings(),
        )
        async with _client(app) as c:
            resp = await c.get("/healthz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        # Default GITHUB_APP_NAME is "Issue Agent" -> slugified.
        assert data["service"] == "issue-agent"
        assert data["version"] == _INSTALLED_VERSION

    @pytest.mark.asyncio
    async def test_service_name_reflects_configured_bot_name(
        self, monkeypatch: pytest.MonkeyPatch, app: FastAPI
    ) -> None:
        _configure(
            monkeypatch,
            app,
            _make_settings(AGENT_NAME="My Custom Bot"),
        )
        async with _client(app) as c:
            resp = await c.get("/healthz")
        data = resp.json()
        assert data["service"] == "my-custom-bot"

    @pytest.mark.asyncio
    async def test_still_returns_200_when_settings_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, app: FastAPI
    ) -> None:
        """Liveness probe must never 500 just because settings can't build."""

        def _boom():
            raise RuntimeError("settings not configured")

        monkeypatch.setattr("issue_agent.webhook.health.get_settings", _boom)
        async with _client(app) as c:
            resp = await c.get("/healthz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["service"] == "issue-agent"


# ── /readyz ─────────────────────────────────────────────────────


class TestReadyzAllOk:
    @pytest.mark.asyncio
    async def test_returns_200_when_everything_ready(
        self, monkeypatch: pytest.MonkeyPatch, app: FastAPI
    ) -> None:
        _configure(
            monkeypatch,
            app,
            _make_settings(),
        )
        async with _client(app) as c:
            resp = await c.get("/readyz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["version"] == _INSTALLED_VERSION
        assert data["checks"]["sqlite"] == "ok"
        assert data["checks"]["settings"] == "ok"
        assert data["checks"]["github_auth"] == "ok"
        assert data["checks"]["llm_function_calling"] == "ok"


class TestReadyzFunctionCallingCheck:
    @pytest.mark.asyncio
    async def test_advisory_only_does_not_flip_readiness(
        self, monkeypatch: pytest.MonkeyPatch, app: FastAPI
    ) -> None:
        _configure(
            monkeypatch,
            app,
            _make_settings(),
        )
        monkeypatch.setattr(
            "issue_agent.webhook.health.check_function_calling",
            lambda model, provider: "LiteLLM reports it may not support function calling",
        )
        async with _client(app) as c:
            resp = await c.get("/readyz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert "warning" in data["checks"]["llm_function_calling"]

    @pytest.mark.asyncio
    async def test_warns_for_a_model_only_a_repo_override_uses(
        self, monkeypatch: pytest.MonkeyPatch, app: FastAPI
    ) -> None:
        """A repo's `llm.model` override must be checked even when the global
        default model supports function calling fine."""
        _configure(
            monkeypatch,
            app,
            _make_settings(
                LLM_MODEL="global/model",
                REPO_ROUTES=[
                    {
                        "issues": "owner/repo",
                        "llm": {"model": "repo/model-without-tools"},
                    }
                ],
            ),
        )
        monkeypatch.setattr(
            "issue_agent.webhook.health.check_function_calling",
            lambda model, provider: "no tools" if model == "repo/model-without-tools" else None,
        )
        async with _client(app) as c:
            resp = await c.get("/readyz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"  # advisory only, never flips readiness
        assert "repo/model-without-tools" in data["checks"]["llm_function_calling"]
        assert "global/model" not in data["checks"]["llm_function_calling"]


class TestReadyzOwnBotLoginCheck:
    @pytest.mark.asyncio
    async def test_warns_when_bot_login_not_configured(
        self, monkeypatch: pytest.MonkeyPatch, app: FastAPI
    ) -> None:
        """Advisory only — a missing github_app_bot_login must never flip
        readiness, but it should be visible so an operator can tell the
        own-bot comment filter (PR #74) may not cover their App's real
        bot login."""
        _configure(
            monkeypatch,
            app,
            _make_settings(PROVIDERS=github_providers(bot_login="")),
        )
        async with _client(app) as c:
            resp = await c.get("/readyz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert "warning" in data["checks"]["github_own_bot_login"]

    @pytest.mark.asyncio
    async def test_ok_when_bot_login_configured(
        self, monkeypatch: pytest.MonkeyPatch, app: FastAPI
    ) -> None:
        _configure(
            monkeypatch,
            app,
            _make_settings(PROVIDERS=github_providers(bot_login="my-app[bot]")),
        )
        async with _client(app) as c:
            resp = await c.get("/readyz")
        data = resp.json()
        assert data["checks"]["github_own_bot_login"] == "ok"


class TestReadyzSqliteFailure:
    @pytest.mark.asyncio
    async def test_returns_503_when_db_unreachable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        app: FastAPI,
    ) -> None:
        _configure(
            monkeypatch,
            app,
            _make_settings(),
        )
        broken = SQLiteStore("/__nonexistent__/queue.db")
        app.state.store = broken
        async with _client(app) as c:
            resp = await c.get("/readyz")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "not_ready"
        assert data["checks"]["sqlite"] != "ok"


class TestReadyzSettingsFailure:
    @pytest.mark.asyncio
    async def test_returns_503_when_webhook_secret_missing(
        self, monkeypatch: pytest.MonkeyPatch, app: FastAPI
    ) -> None:
        _configure(
            monkeypatch,
            app,
            _make_settings(PROVIDERS=github_providers(webhook_secret="")),
        )
        async with _client(app) as c:
            resp = await c.get("/readyz")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "not_ready"
        assert "PROVIDERS__GITHUB__WEBHOOK_SECRET" in data["checks"]["settings"]

    @pytest.mark.asyncio
    async def test_returns_503_when_llm_key_missing(
        self, monkeypatch: pytest.MonkeyPatch, app: FastAPI
    ) -> None:
        _configure(
            monkeypatch,
            app,
            _make_settings(LLM_API_KEY=""),
        )
        async with _client(app) as c:
            resp = await c.get("/readyz")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "not_ready"
        assert "LLM_API_KEY" in data["checks"]["settings"]

    @pytest.mark.asyncio
    async def test_returns_503_when_repo_routes_empty(
        self, monkeypatch: pytest.MonkeyPatch, app: FastAPI
    ) -> None:
        """/readyz no longer depends on GITHUB_INSTALLATION_ID — it fails
        when repo_routes (config.yaml: repos) is empty instead."""
        _configure(
            monkeypatch,
            app,
            _make_settings(REPO_ROUTES=[]),
        )
        async with _client(app) as c:
            resp = await c.get("/readyz")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "not_ready"
        assert "REPO_ROUTES" in data["checks"]["settings"]


class TestReadyzGithubAuthFailure:
    @pytest.mark.asyncio
    async def test_returns_503_when_private_key_empty(
        self, monkeypatch: pytest.MonkeyPatch, app: FastAPI
    ) -> None:
        _configure(
            monkeypatch,
            app,
            _make_settings(PROVIDERS=github_providers(app_private_key="", app_private_key_path="")),
        )
        async with _client(app) as c:
            resp = await c.get("/readyz")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "not_ready"
        assert "private key not loaded" in data["checks"]["github_auth"]

    @pytest.mark.asyncio
    async def test_returns_503_when_private_key_not_pem(
        self, monkeypatch: pytest.MonkeyPatch, app: FastAPI
    ) -> None:
        _configure(
            monkeypatch,
            app,
            _make_settings(PROVIDERS=github_providers(app_private_key="not-a-pem")),
        )
        async with _client(app) as c:
            resp = await c.get("/readyz")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "not_ready"
        assert "does not look like a valid PEM" in data["checks"]["github_auth"]


class TestReadyzProviders:
    """Each provider instance reports its own health (PRD §3.4)."""

    @pytest.mark.asyncio
    async def test_provider_config_joins_the_settings_check(
        self, monkeypatch: pytest.MonkeyPatch, app: FastAPI
    ) -> None:
        _configure(
            monkeypatch, app, _make_settings(PROVIDERS=github_providers(app_id=0), LLM_API_KEY="")
        )
        async with _client(app) as c:
            resp = await c.get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["checks"]["settings"] == "missing: providers.github.app_id, LLM_API_KEY"

    @pytest.mark.asyncio
    async def test_any_unhealthy_instance_flips_readiness(
        self, monkeypatch: pytest.MonkeyPatch, app: FastAPI
    ) -> None:
        settings = _make_settings()
        _configure(monkeypatch, app, settings)
        app.state.registry = ProviderRegistry(
            {
                "github": GitHubProviderPlugin.from_settings(
                    "github", settings.providers["github"], settings
                ),
                "forge-b": FakeProvider(
                    "forge-b",
                    secret="s",
                    health=ProviderHealth(
                        ok=False, checks={"config": "ok", "auth": "token expired"}
                    ),
                ),
            }
        )
        async with _client(app) as c:
            resp = await c.get("/readyz")
        assert resp.status_code == 503
        checks = resp.json()["checks"]
        assert checks["forge-b_auth"] == "token expired"
        assert checks["github_auth"] == "ok"
        assert checks["settings"] == "ok"
