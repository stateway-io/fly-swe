"""Conformance suite every provider plugin must pass (PRD §5).

Parametrized over every built-in type plus the ``FakeProvider`` variants
(with and without author association, with and without a PR API), through
each one's kit (``kits.py``). It covers the inbound webhook port and the
parts of the outbound ports that need no live forge: URL and reference
syntax, git credentials, PR-API coherence and health.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.core.models import Ignored, TriggerEvent, TriggerKind
from issue_agent.policy.ingestion import ingestion_filter
from issue_agent.providers.base import (
    ProviderCapabilities,
    ProviderHealth,
    ProviderPlugin,
    WebhookRejectedError,
)
from issue_agent.providers.registry import BUILTIN_PROVIDER_TYPES
from tests.helpers.settings import github_providers
from tests.unit.providers.kits import KITS, ProviderKit

SECRET = "contract-secret"
INSTANCE = "under-test"
TARGET = "org/target"
ISSUES = "org/issues"


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


@pytest.fixture(params=sorted(KITS), ids=sorted(KITS))
def kit(request: Any) -> ProviderKit:
    return KITS[request.param]


@pytest.fixture
def plugin(kit: ProviderKit) -> ProviderPlugin:
    return kit.make(INSTANCE, SECRET)


def _parse(kit: ProviderKit, plugin: ProviderPlugin, sample: str) -> TriggerEvent:
    d = kit.delivery(sample, secret=SECRET)
    plugin.webhook.verify_signature(d.headers, d.body)
    result = plugin.webhook.parse_event(d.headers, d.body).result
    assert isinstance(result, TriggerEvent), result
    return result


def test_every_builtin_type_has_a_kit():
    assert set(BUILTIN_PROVIDER_TYPES) <= {k.type for k in KITS.values()}


class TestIdentity:
    def test_type_and_capabilities(self, kit: ProviderKit, plugin: ProviderPlugin):
        assert plugin.type == kit.type
        assert isinstance(plugin.capabilities, ProviderCapabilities)
        if plugin.capabilities.draft_mode == "title_prefix":
            assert plugin.capabilities.draft_prefix

    def test_instance_id(self, plugin: ProviderPlugin):
        assert plugin.instance_id == INSTANCE

    def test_bot_identity_is_self(self, plugin: ProviderPlugin):
        bot = plugin.bot_identity()
        assert bot.login
        assert bot.is_self is True


class TestSignature:
    def test_valid_signature_passes(self, kit: ProviderKit, plugin: ProviderPlugin):
        d = kit.delivery("issue_opened", secret=SECRET)

        plugin.webhook.verify_signature(d.headers, d.body)

    def test_wrong_secret_is_rejected_without_burning_the_delivery(
        self, kit: ProviderKit, plugin: ProviderPlugin
    ):
        d = kit.delivery("issue_opened", secret="not-the-secret")

        with pytest.raises(WebhookRejectedError) as exc:
            plugin.webhook.verify_signature(d.headers, d.body)
        assert exc.value.mark_reason is None

    def test_tampered_body_is_rejected(self, kit: ProviderKit, plugin: ProviderPlugin):
        d = kit.delivery("issue_opened", secret=SECRET)

        with pytest.raises(WebhookRejectedError):
            plugin.webhook.verify_signature(d.headers, d.body + b" ")

    def test_empty_secret_fails_closed(self, kit: ProviderKit):
        # Signed with the very same empty secret: still nothing is authentic.
        plugin = kit.make(INSTANCE, "")
        d = kit.delivery("issue_opened", secret="")

        with pytest.raises(WebhookRejectedError):
            plugin.webhook.verify_signature(d.headers, d.body)


class TestDeliveryKey:
    def test_stable_across_redeliveries(self, kit: ProviderKit, plugin: ProviderPlugin):
        first = kit.delivery("issue_opened", secret=SECRET)
        again = kit.delivery("issue_opened", secret=SECRET)

        key = plugin.webhook.delivery_key(first.headers, first.body)
        assert key
        assert plugin.webhook.delivery_key(again.headers, again.body) == key

    def test_missing_key_is_rejected(self, kit: ProviderKit, plugin: ProviderPlugin):
        d = kit.delivery("issue_opened", secret=SECRET, delivery_key=None)

        with pytest.raises(WebhookRejectedError) as exc:
            plugin.webhook.delivery_key(d.headers, d.body)
        assert exc.value.mark_reason is None


class TestParseEvent:
    def test_issue_opened(self, kit: ProviderKit, plugin: ProviderPlugin):
        d = kit.delivery("issue_opened", secret=SECRET)
        trigger = _parse(kit, plugin, "issue_opened")

        assert trigger.kind is TriggerKind.ISSUE_OPENED
        assert trigger.target_kind == "issue"
        assert trigger.issue.provider_instance == INSTANCE
        assert trigger.issue.repo
        assert trigger.issue.number > 0
        assert trigger.delivery_key == plugin.webhook.delivery_key(d.headers, d.body)
        assert ingestion_filter(trigger, _settings()) is None

    def test_roundtrips_through_trigger_json(self, kit: ProviderKit, plugin: ProviderPlugin):
        trigger = _parse(kit, plugin, "comment")

        assert TriggerEvent.model_validate_json(trigger.model_dump_json()) == trigger

    def test_comment(self, kit: ProviderKit, plugin: ProviderPlugin):
        trigger = _parse(kit, plugin, "comment")

        assert trigger.kind is TriggerKind.COMMENT_COMMAND
        assert trigger.trigger_comment is not None
        author = trigger.trigger_comment.author
        assert author.is_self is False
        if kit.reports_association:
            assert author.association != "unknown"
        else:
            assert author.association == "unknown"

    def test_comment_on_pr_is_marked_and_dropped_by_the_core(
        self, kit: ProviderKit, plugin: ProviderPlugin
    ):
        trigger = _parse(kit, plugin, "comment_on_pr")

        assert trigger.target_kind == "pr"
        assert ingestion_filter(trigger, _settings()) == "comment_on_pull_request"

    def test_own_bot_comment_is_self_and_dropped_by_the_core(
        self, kit: ProviderKit, plugin: ProviderPlugin
    ):
        trigger = _parse(kit, plugin, "own_bot_comment")

        assert trigger.trigger_comment is not None
        assert trigger.trigger_comment.author.is_self is True
        assert ingestion_filter(trigger, _settings()) == "own_bot_comment"

    def test_labels_changed_without_trigger_label_is_a_core_no_op(
        self, kit: ProviderKit, plugin: ProviderPlugin
    ):
        trigger = _parse(kit, plugin, "labels_changed_without_trigger")

        assert trigger.kind is TriggerKind.LABEL_ADDED
        assert ingestion_filter(trigger, _settings()) == "no_trigger_label"

    def test_unsupported_event_is_ignored(self, kit: ProviderKit, plugin: ProviderPlugin):
        d = kit.delivery("unsupported", secret=SECRET)

        result = plugin.webhook.parse_event(d.headers, d.body).result

        assert isinstance(result, Ignored)

    def test_malformed_body_is_rejected(self, kit: ProviderKit, plugin: ProviderPlugin):
        d = kit.delivery("issue_opened", secret=SECRET)

        with pytest.raises(WebhookRejectedError):
            plugin.webhook.parse_event(d.headers, b"[1, 2, 3]")


class TestTransport:
    def test_clone_url_carries_no_credential(self, plugin: ProviderPlugin):
        transport = plugin.transport()
        url = transport.clone_url(TARGET)

        assert url.startswith("https://")
        assert TARGET in url
        assert "@" not in url
        for value in transport.git_auth_env(TARGET).values():
            assert value not in url

    def test_git_auth_env_is_scoped_and_non_interactive(self, plugin: ProviderPlugin):
        env = plugin.transport().git_auth_env(TARGET)

        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_KEY_0"].startswith("http.https://")
        assert env["GIT_CONFIG_KEY_0"].endswith(".extraHeader")
        assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: ")

    def test_web_urls(self, plugin: ProviderPlugin):
        transport = plugin.transport()

        assert transport.pr_url(TARGET, 12).startswith("https://")
        assert TARGET in transport.pr_url(TARGET, 12)
        assert transport.pr_url(TARGET, 12).endswith("12")
        compare = transport.compare_url(TARGET, "main", "agent/issue-3-fix")
        assert compare.startswith("https://")
        assert "main" in compare
        assert compare.endswith("agent/issue-3-fix")

    @pytest.mark.parametrize("kind", ["issue", "pr"])
    def test_format_ref_qualifies_other_repos(self, plugin: ProviderPlugin, kind: Any):
        transport = plugin.transport()
        local = transport.format_ref(kind, 3, repo=TARGET, from_repo=TARGET)
        remote = transport.format_ref(kind, 3, repo=ISSUES, from_repo=TARGET)

        assert local.endswith("3")
        assert TARGET not in local
        assert remote.startswith(ISSUES)
        assert remote.endswith(local)

    def test_closing_reference_only_with_an_auto_close_keyword(self, plugin: ProviderPlugin):
        transport = plugin.transport()
        closing = transport.closing_reference(ISSUES, 3, TARGET)
        keyword = plugin.capabilities.auto_close_keyword

        if keyword is None:
            assert closing is None
        else:
            assert closing is not None
            assert closing.startswith(keyword)
            assert closing.endswith(transport.format_ref("issue", 3, repo=ISSUES, from_repo=TARGET))


class TestPullRequestPort:
    def test_pulls_matches_the_capability(self, plugin: ProviderPlugin):
        has_pulls = plugin.pulls(TARGET) is not None

        assert has_pulls is plugin.capabilities.supports_pr_api

    def test_client_is_bound_to_a_repo(self, plugin: ProviderPlugin):
        assert plugin.client(TARGET) is not None


class TestHealth:
    def test_reports_without_raising(self, plugin: ProviderPlugin):
        health = plugin.health_check()

        assert isinstance(health, ProviderHealth)
        assert health.checks
        assert all(isinstance(v, str) for v in health.checks.values())
