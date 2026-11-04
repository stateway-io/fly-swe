from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.runner import llm_support
from issue_agent.webhook import function_calling
from issue_agent.webhook.function_calling import PENDING, FunctionCallingCheck
from tests.helpers.settings import github_providers


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


def _make_settings(**overrides) -> Settings:
    defaults = dict(PROVIDERS=github_providers(), LLM_API_KEY="sk-test")
    defaults.update(overrides)
    return SettingsNoEnv(**defaults)


@pytest.fixture
def fake_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    log = MagicMock()
    monkeypatch.setattr(function_calling, "logger", log)
    return log


def _warned_models(log: MagicMock) -> list[str]:
    return [
        c.kwargs["model"]
        for c in log.warning.call_args_list
        if c.args[:1] == ("llm_function_calling_unsupported",)
    ]


class TestFunctionCallingCheck:
    def test_pending_until_run(self) -> None:
        assert FunctionCallingCheck(_make_settings()).result == PENDING

    def test_ok_when_every_model_supports_tools(
        self, monkeypatch: pytest.MonkeyPatch, fake_logger: MagicMock
    ) -> None:
        monkeypatch.setattr(llm_support, "check_function_calling", lambda model, provider: None)
        check = FunctionCallingCheck(_make_settings())
        check.run()
        assert check.result == "ok"
        assert _warned_models(fake_logger) == []

    def test_warns_only_for_the_model_without_tools(
        self, monkeypatch: pytest.MonkeyPatch, fake_logger: MagicMock
    ) -> None:
        """A repo's `llm.model` override is checked even when the global
        default is healthy, and each distinct model is checked once."""
        seen: list[str] = []

        def _check(model: str, provider: str) -> str | None:
            seen.append(model)
            return "no tools" if model == "repo/model-without-tools" else None

        monkeypatch.setattr(llm_support, "check_function_calling", _check)
        settings = _make_settings(
            LLM_MODEL="global/model",
            REPO_ROUTES=[
                {"issues": "owner/a", "llm": {"model": "repo/model-without-tools"}},
                {"issues": "owner/b", "llm": {"model": "repo/model-without-tools"}},
                {"issues": "owner/c"},
            ],
        )
        check = FunctionCallingCheck(settings)
        check.run()
        assert seen == ["global/model", "repo/model-without-tools"]
        assert check.result == "warning: repo/model-without-tools: no tools"
        assert _warned_models(fake_logger) == ["repo/model-without-tools"]

    def test_a_crash_becomes_a_warning_not_an_exception(
        self, monkeypatch: pytest.MonkeyPatch, fake_logger: MagicMock
    ) -> None:
        def _boom(model: str, explicit_provider: str) -> str:
            raise RuntimeError("litellm exploded")

        monkeypatch.setattr(llm_support, "resolve_provider", _boom)
        check = FunctionCallingCheck(_make_settings())
        check.run()
        assert check.result.startswith("warning: could not check")
        assert "litellm exploded" in check.result
        fake_logger.warning.assert_any_call(
            "llm_function_calling_check_failed", error="litellm exploded"
        )

    def test_start_runs_the_check_in_a_daemon_thread(
        self, monkeypatch: pytest.MonkeyPatch, fake_logger: MagicMock
    ) -> None:
        monkeypatch.setattr(llm_support, "check_function_calling", lambda model, provider: None)
        check = FunctionCallingCheck(_make_settings())
        thread = check.start()
        assert thread.daemon
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert check.result == "ok"
