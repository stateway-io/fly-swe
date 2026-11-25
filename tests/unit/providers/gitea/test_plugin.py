"""``GiteaProviderPlugin``: wiring, lazy API, capabilities and health."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.providers.gitea.api import GiteaAPI
from issue_agent.providers.gitea.client import GiteaForgeClient
from issue_agent.providers.gitea.plugin import GiteaProviderPlugin
from issue_agent.providers.gitea.pr import GiteaPullRequestClient
from issue_agent.providers.gitea.settings import GiteaProviderSettings
from issue_agent.providers.gitea.transport import GiteaTransport
from issue_agent.providers.gitea.webhook import GiteaWebhookAdapter

if TYPE_CHECKING:
    from pathlib import Path

INSTANCE = "gitea_corp"


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(env_file=None, env_prefix="", extra="ignore", yaml_file=None)


def _config(**kw: Any) -> GiteaProviderSettings:
    values: dict[str, Any] = {
        "type": "gitea",
        "host": "https://example.com/git",
        "bot_login": "issue-agent",
        "token": "tok",
        "webhook_secret": "whs",
    }
    values.update(kw)
    return GiteaProviderSettings(**values)


def _plugin(**kw: Any) -> GiteaProviderPlugin:
    return GiteaProviderPlugin(
        INSTANCE, _config(**kw), http_transport=httpx.MockTransport(lambda r: httpx.Response(404))
    )


def test_capabilities():
    caps = GiteaProviderPlugin.capabilities

    assert GiteaProviderPlugin.type == "gitea"
    assert (caps.draft_mode, caps.draft_prefix) == ("title_prefix", "WIP: ")
    assert caps.supports_pr_api is True
    assert caps.supports_labels is True
    assert caps.supports_ci is False
    assert caps.auto_close_keyword == "Closes"
    assert caps.max_body_length == 65_000
    assert caps.terms.pr == "Pull Request"
    assert caps.terms.repo_instructions_files == []


def test_from_settings_takes_the_pr_label_from_the_root_config():
    settings = SettingsNoEnv(
        PROVIDERS={INSTANCE: _config().model_dump()},
        PR_LABEL_AGENT_GENERATED="bot-made",
        LLM_API_KEY="k",
    )

    plugin = GiteaProviderPlugin.from_settings(INSTANCE, settings.providers[INSTANCE], settings)

    assert plugin.instance_id == INSTANCE
    assert plugin._label_agent_generated == "bot-made"
    assert isinstance(plugin.webhook, GiteaWebhookAdapter)


def test_bot_identity():
    me = _plugin().bot_identity()

    assert (me.login, me.association, me.is_self) == ("issue-agent", "bot", True)


class TestOutbound:
    def test_api_is_lazy_and_shared(self, monkeypatch: pytest.MonkeyPatch):
        built: list[GiteaAPI] = []
        real = GiteaAPI.__init__

        def spy(self: GiteaAPI, *a: Any, **kw: Any) -> None:
            real(self, *a, **kw)
            built.append(self)

        monkeypatch.setattr(GiteaAPI, "__init__", spy)
        plugin = _plugin()
        assert built == []

        client = plugin.client("team/app")
        pulls = plugin.pulls("team/app")

        assert isinstance(client, GiteaForgeClient)
        assert isinstance(pulls, GiteaPullRequestClient)
        assert len(built) == 1
        assert client._api is pulls._api is built[0]

    def test_client_carries_the_permission_mode(self):
        client = _plugin(author_permission="api").client("team/app")

        assert client._author_permission == "api"
        assert client._bot_login == "issue-agent"

    def test_bad_ca_bundle_fails_on_first_outbound_use_only(self, tmp_path: Path):
        plugin = _plugin(ca_bundle_path=str(tmp_path / "missing.pem"))

        assert plugin.health_check().checks["tls"].startswith("missing:")
        plugin.transport()  # no API needed
        with pytest.raises(OSError):
            plugin.client("team/app")

    def test_transport_uses_the_configured_host_and_bundle(self):
        transport = _plugin(ca_bundle_path="/data/ca.pem").transport()

        assert isinstance(transport, GiteaTransport)
        assert transport.clone_url("team/app") == "https://example.com/git/team/app.git"
        env = transport.git_auth_env("team/app")
        assert env["GIT_CONFIG_VALUE_1"] == "/data/ca.pem"

    def test_pulls_carry_the_agent_label(self):
        assert _plugin().pulls("team/app")._label_agent_generated == "agent-generated"


class TestHealth:
    def test_all_ok(self, tmp_path: Path):
        bundle = tmp_path / "ca.pem"
        bundle.write_text("pem")

        health = _plugin(ca_bundle_path=str(bundle)).health_check()

        assert health.ok is True
        assert health.checks == {"config": "ok", "tls": "ok", "author_permission": "ok"}

    def test_reports_every_missing_key_by_its_real_name(self):
        health = _plugin(host="", bot_login="", token="", webhook_secret="").health_check()

        assert health.ok is False
        assert health.checks["config"] == (
            "missing: providers.gitea_corp.host, providers.gitea_corp.bot_login, "
            "PROVIDERS__GITEA_CORP__TOKEN, PROVIDERS__GITEA_CORP__WEBHOOK_SECRET"
        )

    def test_only_the_missing_ones(self):
        health = _plugin(token="").health_check()

        assert health.checks["config"] == "missing: PROVIDERS__GITEA_CORP__TOKEN"

    def test_missing_bundle_fails(self, tmp_path: Path):
        path = str(tmp_path / "nope.pem")

        health = _plugin(ca_bundle_path=path).health_check()

        assert health.ok is False
        assert health.checks["tls"] == f"missing: providers.gitea_corp.ca_bundle_path ({path})"

    def test_bundle_must_be_a_file(self, tmp_path: Path):
        assert _plugin(ca_bundle_path=str(tmp_path)).health_check().ok is False

    def test_api_mode_is_a_warning_only(self):
        health = _plugin(author_permission="api").health_check()

        assert health.ok is True
        assert health.checks["author_permission"].startswith("warning: ")

    def test_makes_no_request(self):
        def fail(request: httpx.Request) -> httpx.Response:
            raise AssertionError("health_check must not call the network")

        plugin = GiteaProviderPlugin(INSTANCE, _config(), http_transport=httpx.MockTransport(fail))

        assert plugin.health_check().checks
