from __future__ import annotations

from issue_agent.runner import llm_support


class TestResolveProvider:
    def test_explicit_provider_wins(self):
        assert llm_support.resolve_provider("anything/at/all", "openrouter") == "openrouter"

    def test_derives_from_model_prefix_when_unset(self):
        assert llm_support.resolve_provider("openrouter/deepseek/deepseek-chat", "") == "openrouter"
        assert llm_support.resolve_provider("anthropic/claude-sonnet-4-5", "") == "anthropic"

    def test_empty_string_when_underivable(self, monkeypatch):
        def _boom(*a, **k):
            raise ValueError("no idea")

        monkeypatch.setattr(llm_support.litellm, "get_llm_provider", _boom)
        assert llm_support.resolve_provider("weird-model", "") == ""


class TestCheckFunctionCalling:
    def test_none_when_supported(self, monkeypatch):
        monkeypatch.setattr(llm_support.litellm, "supports_function_calling", lambda **k: True)
        assert (
            llm_support.check_function_calling("anthropic/claude-sonnet-4-5", "anthropic") is None
        )

    def test_warns_when_unsupported(self, monkeypatch):
        monkeypatch.setattr(llm_support.litellm, "supports_function_calling", lambda **k: False)
        msg = llm_support.check_function_calling("some/model", "some")
        assert msg is not None
        assert "function calling" in msg
        assert "some/model" in msg

    def test_warns_softly_when_unverifiable(self, monkeypatch):
        def _boom(**k):
            raise Exception("model not in litellm map")

        monkeypatch.setattr(llm_support.litellm, "supports_function_calling", _boom)
        msg = llm_support.check_function_calling("exotic/model", "exotic")
        assert msg is not None
        assert "could not verify" in msg.lower()

    def test_passes_provider_to_litellm(self, monkeypatch):
        seen = {}

        def _spy(*, model, custom_llm_provider=None):
            seen["model"] = model
            seen["provider"] = custom_llm_provider
            return True

        monkeypatch.setattr(llm_support.litellm, "supports_function_calling", _spy)
        llm_support.check_function_calling("openrouter/x/y", "openrouter")
        assert seen == {"model": "openrouter/x/y", "provider": "openrouter"}

    def test_no_provider_passed_as_none(self, monkeypatch):
        seen = {}

        def _spy(*, model, custom_llm_provider=None):
            seen["provider"] = custom_llm_provider
            return True

        monkeypatch.setattr(llm_support.litellm, "supports_function_calling", _spy)
        llm_support.check_function_calling("openrouter/x/y", "")
        assert seen["provider"] is None
