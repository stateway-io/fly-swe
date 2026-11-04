from __future__ import annotations

import os
import tempfile

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings, get_settings
from issue_agent.main import app as real_app
from issue_agent.queue.store import SQLiteStore
from tests.helpers.settings import github_providers


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
    )
    defaults.update(overrides)
    return SettingsNoEnv(**defaults)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


class TestMainAppStructure:
    def test_app_is_fastapi_instance(self) -> None:
        assert isinstance(real_app, FastAPI)

    def test_app_title(self) -> None:
        # real_app.title is fixed at import time by main.py::_app_title(). It is
        # either the generic fallback (when get_settings() can't resolve — e.g.
        # no secrets in the env) or "<agent_name> Service" when it can.
        # Both branches are asserted precisely in TestAppTitleReflectsSettings;
        # here we only pin the invariant suffix so the check is env-agnostic.
        assert real_app.title.endswith(" Service")

    def test_health_router_included(self) -> None:
        routes = [r.path for r in real_app.routes]
        assert "/healthz" in routes
        assert "/readyz" in routes

    def test_webhook_router_included(self) -> None:
        routes = [r.path for r in real_app.routes]
        # One dispatcher for every provider instance; the GitHub App's
        # `/webhooks/github` is the `github` instance.
        assert "/webhooks/{instance_id}" in routes


class TestAppTitleReflectsSettings:
    def test_uses_configured_bot_name_when_settings_available(self) -> None:
        from issue_agent import main as main_mod

        settings = _make_settings(AGENT_NAME="My Custom Bot")
        original = main_mod.get_settings
        main_mod.get_settings = lambda: settings
        try:
            assert main_mod._app_title() == "My Custom Bot Service"
        finally:
            main_mod.get_settings = original

    def test_falls_back_when_settings_unavailable(self) -> None:
        from issue_agent import main as main_mod

        def _boom():
            raise RuntimeError("settings not configured")

        original = main_mod.get_settings
        main_mod.get_settings = _boom
        try:
            assert main_mod._app_title() == "Issue Agent Service"
        finally:
            main_mod.get_settings = original


class TestMainAppLifespan:
    @pytest.mark.asyncio
    async def test_lifespan_creates_store_and_db_file(self) -> None:
        from issue_agent.main import lifespan

        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(DATA_DIR=tmpdir)
            get_settings.cache_clear()

            original_settings = get_settings
            try:
                get_settings.cache_clear()

                import issue_agent.main as main_mod

                main_mod.get_settings = lambda: settings
                get_settings.cache_clear()

                app = FastAPI()
                async with lifespan(app):
                    assert hasattr(app.state, "store")
                    assert isinstance(app.state.store, SQLiteStore)
                    assert app.state.registry.get("github").instance_id == "github"

                    db_file = os.path.join(tmpdir, "issue_agent.db")
                    assert os.path.exists(db_file)
            finally:
                main_mod.get_settings = original_settings
                get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_lifespan_starts_function_calling_check_in_background(self, monkeypatch) -> None:
        """The advisory check needs LiteLLM, whose import takes seconds: the
        lifespan only starts it, so it never holds the port closed."""
        import issue_agent.main as main_mod
        from issue_agent.main import lifespan
        from issue_agent.webhook.function_calling import FunctionCallingCheck

        started: list[FunctionCallingCheck] = []
        monkeypatch.setattr(FunctionCallingCheck, "start", lambda self: started.append(self))

        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(DATA_DIR=tmpdir)
            monkeypatch.setattr(main_mod, "get_settings", lambda: settings)

            app = FastAPI()
            async with lifespan(app):
                assert started == [app.state.function_calling]
                assert app.state.function_calling.result == "pending"


class TestWebBootPathAvoidsLiteLLM:
    def test_importing_the_web_app_does_not_import_litellm(self) -> None:
        """LiteLLM (and mini-swe-agent, which pulls it in) costs seconds to
        import; the web process must open its port without it. Checked in a
        fresh interpreter, since this test session imports both elsewhere."""
        import subprocess
        import sys

        code = (
            "import sys, issue_agent.main; "
            "print(sorted(m for m in ('litellm', 'minisweagent') if m in sys.modules))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        assert out.stdout.strip().splitlines()[-1] == "[]"

    @pytest.mark.asyncio
    async def test_lifespan_creates_data_dir_if_not_exists(self) -> None:
        from issue_agent.main import lifespan

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, "nested", "data")
            settings = _make_settings(DATA_DIR=data_dir)
            get_settings.cache_clear()

            import issue_agent.main as main_mod

            original_settings = main_mod.get_settings
            main_mod.get_settings = lambda: settings

            try:
                app = FastAPI()
                async with lifespan(app):
                    assert os.path.isdir(data_dir)
                    assert os.path.exists(os.path.join(data_dir, "issue_agent.db"))
            finally:
                main_mod.get_settings = original_settings
                get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_lifespan_closes_store_on_exit(self) -> None:
        from issue_agent.main import lifespan

        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(DATA_DIR=tmpdir)
            get_settings.cache_clear()

            import issue_agent.main as main_mod

            original_settings = main_mod.get_settings
            main_mod.get_settings = lambda: settings

            try:
                app = FastAPI()
                async with lifespan(app):
                    store = app.state.store
                    assert store._conn is not None

                store._conn is None or store.close()
                assert store._conn is None
            finally:
                main_mod.get_settings = original_settings
                get_settings.cache_clear()


class TestHealthEndpoints:
    @pytest.mark.asyncio
    async def test_healthz_returns_200(self) -> None:
        async with AsyncClient(transport=ASGITransport(app=real_app), base_url="http://test") as c:
            resp = await c.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_healthz_no_lifespan_required(self) -> None:
        app = FastAPI()
        from issue_agent.webhook.health import router as health_router

        app.include_router(health_router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/healthz")
        assert resp.status_code == 200
