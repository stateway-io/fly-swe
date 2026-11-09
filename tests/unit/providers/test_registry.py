from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.providers.base import WebhookRejectedError
from issue_agent.providers.github.plugin import GitHubProviderPlugin
from issue_agent.providers.registry import BUILTIN_PROVIDER_TYPES, ProviderRegistry
from tests.fakes.fake_provider import FakeProvider, FakeProviderWithCI, FakeProviderWithPulls
from tests.helpers.github import sign_payload
from tests.helpers.settings import github_providers


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


def _settings() -> Settings:
    return SettingsNoEnv(
        PROVIDERS=github_providers(app_id=1, app_private_key="k", webhook_secret="s"),
        LLM_API_KEY="k",
    )


def test_builtin_types_are_keyed_by_their_type():
    for name, plugin_cls in BUILTIN_PROVIDER_TYPES.items():
        assert plugin_cls.type == name


class TestFromSettings:
    def test_single_github_instance(self):
        registry = ProviderRegistry.from_settings(_settings())

        plugin = registry.get("github")
        assert isinstance(plugin, GitHubProviderPlugin)
        assert [p.instance_id for p in registry.all()] == ["github"]

    def test_two_github_instances_keep_their_own_config(self):
        settings = SettingsNoEnv(
            PROVIDERS={
                **github_providers(app_id=1, webhook_secret="s1"),
                **github_providers(
                    "ghe", app_id=2, webhook_secret="s2", host="https://ghe.example.com"
                ),
            },
            LLM_API_KEY="k",
        )

        with patch("issue_agent.providers.github.auth.GithubIntegration"):
            registry = ProviderRegistry.from_settings(settings)
            github, ghe = registry.get("github"), registry.get("ghe")
            assert github.transport().clone_url("o/r") == "https://github.com/o/r.git"
            assert ghe.transport().clone_url("o/r") == "https://ghe.example.com/o/r.git"

        assert (github.instance_id, ghe.instance_id) == ("github", "ghe")
        body = b"{}"
        ghe.webhook.verify_signature({"X-Hub-Signature-256": sign_payload(body, "s2")}, body)
        with pytest.raises(WebhookRejectedError):
            github.webhook.verify_signature({"X-Hub-Signature-256": sign_payload(body, "s2")}, body)

    def test_no_instances_is_an_empty_registry(self):
        settings = SettingsNoEnv(LLM_API_KEY="k")

        assert list(ProviderRegistry.from_settings(settings).all()) == []

    def test_unknown_instance_raises_key_error(self):
        with pytest.raises(KeyError):
            ProviderRegistry.from_settings(_settings()).get("gitlab")


def test_instance_id_must_match_its_key():
    settings = _settings()
    plugin = GitHubProviderPlugin.from_settings("github", settings.providers["github"], settings)

    with pytest.raises(ValueError, match="registered as 'other'"):
        ProviderRegistry({"other": plugin})


class TestPullRequestCapabilityCoherence:
    """`supports_pr_api` and overriding `pulls()` must agree (PRD §3.2), so
    the core can trust `pulls(repo) is None` to mean "no PR API"."""

    def test_fakes_with_and_without_pulls_are_accepted(self):
        ProviderRegistry(
            {
                "fake": FakeProvider("fake", secret="s"),
                "mr": FakeProviderWithPulls("mr", secret="s"),
            }
        )

    def test_pulls_without_the_capability_is_rejected(self):
        class Inconsistent(FakeProviderWithPulls):
            capabilities = FakeProvider.capabilities

        with pytest.raises(ValueError, match="supports_pr_api=False but implements"):
            ProviderRegistry({"x": Inconsistent("x", secret="s")})

    def test_capability_without_pulls_is_rejected(self):
        class Inconsistent(FakeProvider):
            capabilities = FakeProviderWithPulls.capabilities

        with pytest.raises(ValueError, match="supports_pr_api=True but does not"):
            ProviderRegistry({"x": Inconsistent("x", secret="s")})


class TestCICapabilityCoherence:
    """`supports_ci` and overriding `ci()` must agree, so the CI monitor
    can trust `ci(repo) is None` to mean "no CI port"."""

    def test_fake_with_ci_is_accepted(self):
        ProviderRegistry({"ci": FakeProviderWithCI("ci", secret="s")})

    def test_ci_without_the_capability_is_rejected(self):
        class Inconsistent(FakeProviderWithCI):
            capabilities = FakeProviderWithPulls.capabilities

        with pytest.raises(ValueError, match="supports_ci=False but implements"):
            ProviderRegistry({"x": Inconsistent("x", secret="s")})

    def test_capability_without_ci_is_rejected(self):
        class Inconsistent(FakeProviderWithPulls):
            capabilities = FakeProviderWithCI.capabilities

        with pytest.raises(ValueError, match="supports_ci=True but does not"):
            ProviderRegistry({"x": Inconsistent("x", secret="s")})
