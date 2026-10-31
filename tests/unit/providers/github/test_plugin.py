from __future__ import annotations

from typing import Any

from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.providers.github.plugin import GitHubProviderPlugin
from issue_agent.providers.github.webhook import GitHubWebhookAdapter
from tests.helpers.settings import github_providers


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file=None, env_prefix="", extra="ignore", yaml_file=None,
    )


def _settings(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "PROVIDERS": github_providers(app_id=1, app_private_key="k", webhook_secret="s"),
        "AGENT_NAME": "Stateway Issue Agent",
        "LLM_API_KEY": "k",
    }
    fields.update(overrides)
    return SettingsNoEnv(**fields)


def _from_settings(settings: Settings) -> GitHubProviderPlugin:
    return GitHubProviderPlugin.from_settings(
        "github", settings.providers["github"], settings
    )


class TestFromSettings:
    def test_builds_the_instance(self):
        plugin = _from_settings(_settings())

        assert plugin.instance_id == "github"
        assert isinstance(plugin.webhook, GitHubWebhookAdapter)

    def test_bot_identity_from_the_app_name(self):
        author = _from_settings(_settings()).bot_identity()

        assert author.login == "stateway-issue-agent[bot]"
        assert author.association == "bot"
        assert author.is_self is True

    def test_bot_identity_prefers_the_explicit_login(self):
        plugin = _from_settings(
            _settings(PROVIDERS=github_providers(bot_login="Custom[bot]"))
        )

        assert plugin.bot_identity().login == "custom[bot]"


def test_capabilities():
    caps = GitHubProviderPlugin.capabilities

    assert GitHubProviderPlugin.type == "github"
    assert caps.supports_pr_api is True
    assert caps.draft_mode == "native"
    assert caps.terms.ref_prefix_issue == "#"
