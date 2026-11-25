"""``GiteaWebhookAdapter`` on the real captured deliveries (PRD-Gitea §4.2)."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.core.models import Ignored, TriggerEvent, TriggerKind
from issue_agent.policy.ingestion import ingestion_filter
from issue_agent.providers.base import WebhookRejectedError
from issue_agent.providers.gitea.webhook import GiteaWebhookAdapter

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "webhook-test-secret"
BOT = "issue-agent"
INSTANCE = "gitea"
REPO = "team/app"


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(env_file=None, env_prefix="", extra="ignore", yaml_file=None)


def _settings() -> Settings:
    return SettingsNoEnv(
        PROVIDERS={INSTANCE: {"type": "gitea", "host": "https://git.example.com"}},
        REPO_ROUTES=[{"issues": REPO}],
        LLM_API_KEY="k",
    )


def _adapter() -> GiteaWebhookAdapter:
    return GiteaWebhookAdapter(instance_id=INSTANCE, secret=SECRET, bot_login=BOT)


def _delivery(
    name: str, *, body: bytes | None = None, **headers: str | None
) -> tuple[dict[str, str], bytes]:
    """A captured delivery, re-signed with ``SECRET``. Keyword headers use
    ``_`` for ``-`` and override (None drops) the captured ones."""
    if body is None:
        body = (FIXTURES / f"{name}.json").read_bytes()
    captured: dict[str, str] = json.loads((FIXTURES / "headers" / f"{name}.json").read_text())
    captured["X-Gitea-Signature"] = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    for key, value in headers.items():
        header = key.replace("_", "-")
        if value is None:
            captured.pop(header, None)
        else:
            captured[header] = value
    return captured, body


def _parse(name: str, **kwargs: Any) -> TriggerEvent | Ignored:
    headers, body = _delivery(name, **kwargs)
    adapter = _adapter()
    adapter.verify_signature(headers, body)
    return adapter.parse_event(headers, body).result


def _trigger(name: str, **kwargs: Any) -> TriggerEvent:
    result = _parse(name, **kwargs)
    assert isinstance(result, TriggerEvent), result
    return result


class TestDeliveryKey:
    def test_is_content_based_and_ignores_the_uuid(self):
        # Gitea's "Replay" sends a new X-Gitea-Delivery with the same body.
        original = _delivery("issues_opened")
        replay = _delivery("issues_opened", X_Gitea_Delivery="11111111-2222-3333-4444-555555555555")
        adapter = _adapter()

        key = adapter.delivery_key(*original)
        assert key.startswith("gitea-")
        assert adapter.delivery_key(*replay) == key

    def test_differs_between_events(self):
        adapter = _adapter()
        keys = {
            adapter.delivery_key(*_delivery(name))
            for name in (
                "issues_opened",
                "issues_label_updated_without_trigger",
                "issues_label_updated_with_trigger",
                "issues_label_cleared",
                "issue_comment_created",
                "issue_comment_own_bot",
            )
        }

        assert len(keys) == 6

    def test_covers_the_event_headers(self):
        adapter = _adapter()
        base = adapter.delivery_key(*_delivery("issues_opened"))

        assert adapter.delivery_key(*_delivery("issues_opened", X_Gitea_Event="push")) != base
        other_type = _delivery("issues_opened", X_Gitea_Event_Type="issue_label")
        assert adapter.delivery_key(*other_type) != base

    def test_does_not_parse_the_body(self):
        # It runs before the signature check: garbage must not raise.
        adapter = _adapter()

        assert adapter.delivery_key({"X-Gitea-Delivery": "u"}, b"\xff not json")

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_missing_uuid_is_rejected_unmarked(self, value: str | None):
        headers, body = _delivery("issues_opened", X_Gitea_Delivery=value)

        with pytest.raises(WebhookRejectedError) as exc:
            _adapter().delivery_key(headers, body)
        assert exc.value.mark_reason is None
        assert exc.value.log_event == "missing_delivery_header"

    def test_github_delivery_header_does_not_count(self):
        headers = {"X-GitHub-Delivery": "u", "X-Gogs-Delivery": "u"}

        with pytest.raises(WebhookRejectedError):
            _adapter().delivery_key(headers, b"{}")


class TestEventName:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("issues_opened", "issues"),
            ("issues_label_updated_with_trigger", "issues/issue_label"),
            ("issue_comment_created", "issue_comment"),
            ("issue_comment_on_pull_request", "issue_comment/pull_request_comment"),
            ("push", "push"),
        ],
    )
    def test_shows_the_event_type_when_it_differs(self, name: str, expected: str):
        headers, _ = _delivery(name)

        assert _adapter().event_name(headers) == expected

    def test_reads_only_gitea_headers(self):
        assert _adapter().event_name({"X-GitHub-Event": "issues"}) == ""

    def test_parsed_delivery_carries_name_and_action(self):
        headers, body = _delivery("issues_label_cleared")

        parsed = _adapter().parse_event(headers, body)

        assert parsed.event == "issues/issue_label"
        assert parsed.action == "label_cleared"


class TestIssueEvents:
    def test_opened(self):
        trigger = _trigger("issues_opened")

        assert trigger.kind is TriggerKind.ISSUE_OPENED
        assert trigger.target_kind == "issue"
        assert trigger.issue.provider_instance == INSTANCE
        assert trigger.issue.repo == REPO
        assert trigger.issue.number == 1
        assert trigger.issue.title == "Crash when the config file is empty"
        assert trigger.issue.author.login == "maintainer"
        assert trigger.issue.author.association == "unknown"
        assert trigger.actor.login == "maintainer"
        assert trigger.trigger_comment is None
        assert trigger.credentials_ref is None
        assert trigger.delivery_key == _adapter().delivery_key(*_delivery("issues_opened"))
        assert ingestion_filter(trigger, _settings()) is None

    def test_label_updated_with_a_trigger_label_is_kept(self):
        trigger = _trigger("issues_label_updated_with_trigger")

        assert trigger.kind is TriggerKind.LABEL_ADDED
        assert trigger.issue.labels == ["agent-fix-please", "wontfix"]
        assert ingestion_filter(trigger, _settings()) is None

    def test_label_updated_without_a_trigger_label_is_a_core_no_op(self):
        trigger = _trigger("issues_label_updated_without_trigger")

        assert trigger.kind is TriggerKind.LABEL_ADDED
        assert trigger.issue.labels == ["wontfix"]
        assert ingestion_filter(trigger, _settings()) == "no_trigger_label"

    def test_label_cleared_ignores_the_stale_labels(self):
        # Gitea still lists the pre-clear labels — a trigger label among
        # them — in a label_cleared payload. Clearing must not trigger.
        payload = json.loads((FIXTURES / "issues_label_cleared.json").read_bytes())
        assert "agent-fix-please" in [lb["name"] for lb in payload["issue"]["labels"]]

        trigger = _trigger("issues_label_cleared")

        assert trigger.kind is TriggerKind.LABEL_ADDED
        assert trigger.issue.labels == []
        assert ingestion_filter(trigger, _settings()) == "no_trigger_label"

    @pytest.mark.parametrize(
        ("name", "kind", "state"),
        [
            ("issues_closed", TriggerKind.ISSUE_CLOSED, "closed"),
            ("issues_reopened", TriggerKind.ISSUE_REOPENED, "open"),
        ],
    )
    def test_closed_and_reopened(self, name: str, kind: TriggerKind, state: str):
        trigger = _trigger(name)

        assert trigger.kind is kind
        assert trigger.issue.state == state

    @pytest.mark.parametrize("action", ["assigned", "milestoned", "edited", None])
    def test_other_issue_actions_are_ignored_with_the_repo(self, action: str | None):
        payload = json.loads((FIXTURES / "issues_opened.json").read_bytes())
        payload["action"] = action

        result = _parse("issues_opened", body=json.dumps(payload).encode())

        assert result == Ignored("unhandled_event_action", REPO)


class TestCommentEvents:
    def test_comment_by_a_maintainer(self):
        trigger = _trigger("issue_comment_created")

        assert trigger.kind is TriggerKind.COMMENT_COMMAND
        assert trigger.target_kind == "issue"
        assert trigger.trigger_comment is not None
        assert trigger.trigger_comment.body == "/agent fix please"
        assert trigger.trigger_comment.author.login == "maintainer"
        assert trigger.trigger_comment.author.association == "unknown"
        assert trigger.trigger_comment.author.is_self is False
        assert ingestion_filter(trigger, _settings()) is None

    def test_own_bot_comment_is_dropped_by_the_core(self):
        trigger = _trigger("issue_comment_own_bot")

        assert trigger.trigger_comment is not None
        assert trigger.trigger_comment.author.is_self is True
        assert ingestion_filter(trigger, _settings()) == "own_bot_comment"

    def test_comment_on_a_pr_is_marked_and_dropped_by_the_core(self):
        trigger = _trigger("issue_comment_on_pull_request")

        assert trigger.target_kind == "pr"
        assert ingestion_filter(trigger, _settings()) == "comment_on_pull_request"

    def test_edited_comment_is_ignored(self):
        assert _parse("issue_comment_edited") == Ignored("unhandled_event_action", REPO)

    def test_comment_without_a_comment_object_still_normalizes(self):
        payload = json.loads((FIXTURES / "issue_comment_created.json").read_bytes())
        del payload["comment"]

        trigger = _trigger("issue_comment_created", body=json.dumps(payload).encode())

        assert trigger.trigger_comment is not None
        assert trigger.trigger_comment.id == ""

    def test_roundtrips_through_trigger_json(self):
        trigger = _trigger("issue_comment_created")

        assert TriggerEvent.model_validate_json(trigger.model_dump_json()) == trigger


class TestUnsupported:
    def test_push_is_ignored_without_a_repo(self):
        headers, body = _delivery("push")

        parsed = _adapter().parse_event(headers, body)

        assert parsed.result == Ignored("unsupported_event", None)
        assert parsed.action is None

    def test_unsupported_event_is_ignored_before_parsing(self):
        headers, _ = _delivery("push")

        assert _adapter().parse_event(headers, b"not json").result == Ignored(
            "unsupported_event", None
        )

    def test_github_event_header_is_not_trusted(self):
        # Only X-Gitea-Event decides; a spoofed X-GitHub-Event doesn't help.
        headers, body = _delivery("push", X_GitHub_Event="issues")

        assert _adapter().parse_event(headers, body).result == Ignored("unsupported_event", None)


class TestMalformed:
    @pytest.mark.parametrize("body", [b"{not json", b"\xff\xfe"])
    def test_invalid_json_is_rejected_and_marked(self, body: bytes):
        headers, _ = _delivery("issues_opened")

        with pytest.raises(WebhookRejectedError) as exc:
            _adapter().parse_event(headers, body)
        assert exc.value.mark_reason == "invalid_json"

    @pytest.mark.parametrize(
        "payload",
        [
            [1, 2, 3],
            {"action": "opened", "repository": {"full_name": REPO}},
            {"action": "opened", "issue": {"number": 1}},
            {"action": "opened", "issue": "x", "repository": {"full_name": REPO}},
        ],
    )
    def test_invalid_payload_is_rejected_and_marked(self, payload: Any):
        headers, _ = _delivery("issues_opened")

        with pytest.raises(WebhookRejectedError) as exc:
            _adapter().parse_event(headers, json.dumps(payload).encode())
        assert exc.value.mark_reason == "invalid_payload"

    def test_model_validation_error_is_invalid_payload(self):
        payload = json.loads((FIXTURES / "issue_comment_created.json").read_bytes())
        payload["comment"]["created_at"] = "yesterday"

        headers, body = _delivery("issue_comment_created", body=json.dumps(payload).encode())
        with pytest.raises(WebhookRejectedError) as exc:
            _adapter().parse_event(headers, body)
        assert exc.value.mark_reason == "invalid_payload"

    def test_non_string_repo_name_becomes_empty(self):
        payload = json.loads((FIXTURES / "issues_opened.json").read_bytes())
        payload["repository"]["full_name"] = 7
        payload["action"] = "assigned"

        assert _parse("issues_opened", body=json.dumps(payload).encode()) == Ignored(
            "unhandled_event_action", ""
        )
