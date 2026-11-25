"""The contract suite's webhook checks, run against ``GiteaKit`` + adapter.

Interim: the full conformance suite needs ``GiteaKit.make`` (a plugin),
which lands with ``plugin.py`` in PRD-Gitea GT3 — then ``GiteaKit`` joins
``KITS`` and this module goes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.core.models import Ignored, TriggerEvent, TriggerKind
from issue_agent.policy.ingestion import ingestion_filter
from issue_agent.providers.base import WebhookRejectedError
from tests.unit.providers.kits import SAMPLES, GiteaKit

if TYPE_CHECKING:
    from issue_agent.providers.gitea.webhook import GiteaWebhookAdapter

SECRET = "contract-secret"
INSTANCE = "under-test"
KIT = GiteaKit()


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(env_file=None, env_prefix="", extra="ignore", yaml_file=None)


def _settings() -> Settings:
    return SettingsNoEnv(
        PROVIDERS={INSTANCE: {"type": "gitea", "host": "https://git.example.com"}},
        LLM_API_KEY="k",
    )


@pytest.fixture
def adapter() -> GiteaWebhookAdapter:
    return KIT.adapter(INSTANCE, SECRET)


def _parse(adapter: GiteaWebhookAdapter, sample: str) -> TriggerEvent:
    d = KIT.delivery(sample, secret=SECRET)
    adapter.verify_signature(d.headers, d.body)
    result = adapter.parse_event(d.headers, d.body).result
    assert isinstance(result, TriggerEvent), result
    return result


def test_kit_covers_every_sample():
    for sample in SAMPLES:
        assert KIT.delivery(sample, secret=SECRET).body


class TestSignature:
    def test_valid_signature_passes(self, adapter):
        d = KIT.delivery("issue_opened", secret=SECRET)

        adapter.verify_signature(d.headers, d.body)

    def test_wrong_secret_is_rejected_without_burning_the_delivery(self, adapter):
        d = KIT.delivery("issue_opened", secret="not-the-secret")

        with pytest.raises(WebhookRejectedError) as exc:
            adapter.verify_signature(d.headers, d.body)
        assert exc.value.mark_reason is None

    def test_tampered_body_is_rejected(self, adapter):
        d = KIT.delivery("issue_opened", secret=SECRET)

        with pytest.raises(WebhookRejectedError):
            adapter.verify_signature(d.headers, d.body + b" ")

    def test_empty_secret_fails_closed(self):
        d = KIT.delivery("issue_opened", secret="")

        with pytest.raises(WebhookRejectedError):
            KIT.adapter(INSTANCE, "").verify_signature(d.headers, d.body)


class TestDeliveryKey:
    def test_stable_across_redeliveries(self, adapter):
        first = KIT.delivery("issue_opened", secret=SECRET)
        again = KIT.delivery("issue_opened", secret=SECRET, delivery_key="delivery-2")

        key = adapter.delivery_key(first.headers, first.body)
        assert key
        assert adapter.delivery_key(again.headers, again.body) == key

    def test_missing_key_is_rejected(self, adapter):
        d = KIT.delivery("issue_opened", secret=SECRET, delivery_key=None)

        with pytest.raises(WebhookRejectedError) as exc:
            adapter.delivery_key(d.headers, d.body)
        assert exc.value.mark_reason is None


class TestParseEvent:
    def test_issue_opened(self, adapter):
        d = KIT.delivery("issue_opened", secret=SECRET)
        trigger = _parse(adapter, "issue_opened")

        assert trigger.kind is TriggerKind.ISSUE_OPENED
        assert trigger.target_kind == "issue"
        assert trigger.issue.provider_instance == INSTANCE
        assert trigger.issue.repo
        assert trigger.issue.number > 0
        assert trigger.delivery_key == adapter.delivery_key(d.headers, d.body)
        assert ingestion_filter(trigger, _settings()) is None

    def test_roundtrips_through_trigger_json(self, adapter):
        trigger = _parse(adapter, "comment")

        assert TriggerEvent.model_validate_json(trigger.model_dump_json()) == trigger

    def test_comment_reports_no_association(self, adapter):
        trigger = _parse(adapter, "comment")

        assert trigger.kind is TriggerKind.COMMENT_COMMAND
        assert trigger.trigger_comment is not None
        assert trigger.trigger_comment.author.is_self is False
        assert trigger.trigger_comment.author.association == "unknown"

    def test_comment_on_pr_is_marked_and_dropped_by_the_core(self, adapter):
        trigger = _parse(adapter, "comment_on_pr")

        assert trigger.target_kind == "pr"
        assert ingestion_filter(trigger, _settings()) == "comment_on_pull_request"

    def test_own_bot_comment_is_self_and_dropped_by_the_core(self, adapter):
        trigger = _parse(adapter, "own_bot_comment")

        assert trigger.trigger_comment is not None
        assert trigger.trigger_comment.author.is_self is True
        assert ingestion_filter(trigger, _settings()) == "own_bot_comment"

    def test_labels_changed_without_trigger_label_is_a_core_no_op(self, adapter):
        trigger = _parse(adapter, "labels_changed_without_trigger")

        assert trigger.kind is TriggerKind.LABEL_ADDED
        assert ingestion_filter(trigger, _settings()) == "no_trigger_label"

    def test_unsupported_event_is_ignored(self, adapter):
        d = KIT.delivery("unsupported", secret=SECRET)

        assert isinstance(adapter.parse_event(d.headers, d.body).result, Ignored)

    def test_malformed_body_is_rejected(self, adapter):
        d = KIT.delivery("issue_opened", secret=SECRET)

        with pytest.raises(WebhookRejectedError):
            adapter.parse_event(d.headers, b"[1, 2, 3]")
