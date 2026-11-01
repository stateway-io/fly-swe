from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.core.models import Ignored, TriggerEvent, TriggerKind
from issue_agent.providers.base import WebhookRejectedError
from issue_agent.providers.github.normalize import bot_identity_from_settings
from issue_agent.providers.github.webhook import (
    GitHubWebhookAdapter,
    event_is_allowed,
    normalize_payload,
)
from tests.helpers.settings import github_providers

APP_ID = 3735121
APP_NAME = "Stateway Issue Agent"
OWN_LOGIN = "stateway-issue-agent[bot]"
MISCONFIGURED_APP_NAME = "issue-agent"
REPO = "stateway-io/stateway-issues"


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


def _settings(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "PROVIDERS": github_providers(app_id=APP_ID, app_private_key="k", webhook_secret="s"),
        "AGENT_NAME": APP_NAME,
        "LLM_API_KEY": "k",
    }
    fields.update(overrides)
    return SettingsNoEnv(**fields)


def _issue(**overrides: Any) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "id": 555,
        "number": 7,
        "title": "t",
        "body": "b",
        "state": "open",
        "labels": [{"name": "bug"}],
        "user": {"login": "reporter", "type": "User"},
        "author_association": "NONE",
        "html_url": f"https://github.com/{REPO}/issues/7",
    }
    issue.update(overrides)
    return issue


def _payload(action: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
        "issue": _issue(),
        "repository": {"full_name": REPO},
        "sender": {"login": "whoever", "type": "User"},
        "installation": {"id": 987654},
    }
    payload.update(overrides)
    return payload


def _comment_payload(comment: dict[str, Any], issue_state: str | None = "open") -> dict[str, Any]:
    full_comment: dict[str, Any] = {
        "id": 1001,
        "body": "please fix",
        "created_at": "2026-01-01T00:00:00Z",
        "user": {"login": "maintainer", "type": "User"},
        "author_association": "MEMBER",
    }
    full_comment.update(comment)
    return _payload("created", comment=full_comment, issue=_issue(state=issue_state))


def _parse(event: str, payload: dict[str, Any], **settings: Any) -> TriggerEvent | Ignored:
    return normalize_payload(
        "d1",
        event,
        payload,
        instance_id="github",
        identity=bot_identity_from_settings(
            _settings(**settings).providers["github"], _settings(**settings).agent_name
        ),
    )


class TestNormalization:
    def test_issue_opened_fields(self):
        result = _parse("issues", _payload("opened"))

        assert isinstance(result, TriggerEvent)
        assert result.kind is TriggerKind.ISSUE_OPENED
        assert result.target_kind == "issue"
        assert result.delivery_key == "d1"
        assert result.credentials_ref == "987654"
        assert result.trigger_comment is None
        issue = result.issue
        assert issue.provider_instance == "github"
        assert issue.repo == REPO
        assert issue.number == 7
        assert issue.global_id == "555"
        assert (issue.title, issue.body) == ("t", "b")
        assert issue.labels == ["bug"]
        assert issue.url == f"https://github.com/{REPO}/issues/7"
        assert issue.author.login == "reporter"
        assert issue.author.association == "none"

    def test_actor_is_the_sender_not_the_issue_author(self):
        # On `labeled`, the sender is whoever added the label.
        result = _parse("issues", _payload("labeled", sender={"login": "triager"}))

        assert isinstance(result, TriggerEvent)
        assert result.actor.login == "triager"
        assert result.issue.author.login == "reporter"

    def test_labels_are_the_current_set(self):
        payload = _payload(
            "labeled",
            issue=_issue(labels=[{"name": "bug"}, {"name": "docs"}, {"bad": 1}, "x"]),
            label={"name": "docs"},
        )
        result = _parse("issues", payload)

        assert isinstance(result, TriggerEvent)
        assert result.issue.labels == ["bug", "docs"]

    def test_comment_command_carries_the_trigger_comment(self):
        result = _parse("issue_comment", _comment_payload({}))

        assert isinstance(result, TriggerEvent)
        assert result.kind is TriggerKind.COMMENT_COMMAND
        comment = result.trigger_comment
        assert comment is not None
        assert comment.id == "1001"
        assert comment.body == "please fix"
        assert comment.created_at is not None
        assert comment.author.login == "maintainer"
        assert comment.author.association == "member"
        assert comment.author.is_self is False

    def test_missing_installation_leaves_credentials_ref_empty(self):
        payload = _payload("opened")
        del payload["installation"]
        result = _parse("issues", payload)

        assert isinstance(result, TriggerEvent)
        assert result.credentials_ref is None

    def test_sparse_payload_still_normalizes(self):
        result = _parse("issues", {"action": "opened", "issue": {"number": 1}})

        assert isinstance(result, TriggerEvent)
        assert result.issue.repo == ""
        assert result.issue.author.login == ""
        assert result.issue.author.association == "unknown"

    def test_malformed_field_raises_validation_error(self):
        with pytest.raises(ValidationError):
            _parse("issues", _payload("opened", issue=_issue(number="not-a-number")))

    def test_roundtrips_through_json(self):
        result = _parse("issue_comment", _comment_payload({}))

        assert isinstance(result, TriggerEvent)
        assert TriggerEvent.model_validate_json(result.model_dump_json()) == result


class TestInferTriggerKind:
    @pytest.mark.parametrize(
        "event,action,expected",
        [
            ("issues", "opened", TriggerKind.ISSUE_OPENED),
            ("issues", "labeled", TriggerKind.LABEL_ADDED),
            ("issue_comment", "created", TriggerKind.COMMENT_COMMAND),
            # R7.1: the lifecycle events are recognized so the
            # handler can close/record `issue_work` without enqueuing a Task.
            ("issues", "closed", TriggerKind.ISSUE_CLOSED),
            ("issues", "reopened", TriggerKind.ISSUE_REOPENED),
        ],
    )
    def test_mapping(self, event, action, expected):
        payload = _comment_payload({}) if event == "issue_comment" else _payload(action)
        result = _parse(event, payload)

        assert isinstance(result, TriggerEvent)
        assert result.kind is expected

    @pytest.mark.parametrize("event,action", [("issues", "deleted"), ("pull_request", "opened")])
    def test_unhandled_actions_are_ignored(self, event, action):
        result = _parse(event, _payload(action))

        assert result == Ignored("unhandled_event_action", REPO)


class TestOwnBotComment:
    def _is_own(self, comment: dict[str, Any], **settings: Any) -> bool:
        result = _parse("issue_comment", _comment_payload(comment), **settings)
        assert isinstance(result, TriggerEvent)
        assert result.trigger_comment is not None
        return result.trigger_comment.author.is_self

    def test_matches_by_performed_via_github_app_id(self):
        assert self._is_own({"performed_via_github_app": {"id": APP_ID}, "user": {"type": "Bot"}})

    def test_matches_by_bot_login_slug(self):
        assert self._is_own({"user": {"type": "Bot", "login": OWN_LOGIN}})

    def test_does_not_match_other_bot(self):
        assert not self._is_own({"user": {"type": "Bot", "login": "dependabot[bot]"}})

    def test_does_not_match_other_app_id(self):
        assert not self._is_own(
            {
                "performed_via_github_app": {"id": 999},
                "user": {"type": "Bot", "login": "other[bot]"},
            }
        )

    def test_does_not_match_human_user(self):
        assert not self._is_own({"user": {"type": "User", "login": "octocat"}})

    def test_matches_by_performed_id_as_string(self):
        # Some proxies/webhook retries serialize the app id as a string.
        assert self._is_own(
            {"performed_via_github_app": {"id": str(APP_ID)}, "user": {"type": "Bot"}}
        )

    def test_app_bot_login_override_when_app_name_differs(self):
        # agent.name="issue-agent" does not slugify to the real bot login
        # "stateway-issue-agent[bot]"; an explicit override must make it match.
        assert self._is_own(
            {"user": {"type": "Bot", "login": OWN_LOGIN}},
            AGENT_NAME=MISCONFIGURED_APP_NAME,
            PROVIDERS=github_providers(bot_login=OWN_LOGIN),
        )

    def test_misconfigured_app_name_without_override_fails(self):
        # Without app_bot_login, falling back to slugifying app_name misses the
        # real bot login. This is the failure mode seen in production.
        assert not self._is_own(
            {"user": {"type": "Bot", "login": OWN_LOGIN}},
            AGENT_NAME=MISCONFIGURED_APP_NAME,
        )


class TestNormalizesWhatTheCoreFilters:
    """The parser only normalizes; ``policy/ingestion.py`` decides."""

    def test_comment_on_pull_request_is_marked_pr(self):
        payload = _comment_payload({})
        payload["issue"]["pull_request"] = {"url": "https://api.github.com/.../pulls/7"}

        result = _parse("issue_comment", payload)

        assert isinstance(result, TriggerEvent)
        assert result.target_kind == "pr"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("open", "open"),
            ("closed", "closed"),
            ("CLOSED", "closed"),
            (None, None),
            ("weird", None),
        ],
    )
    def test_issue_state(self, raw, expected):
        result = _parse("issue_comment", _comment_payload({}, issue_state=raw))

        assert isinstance(result, TriggerEvent)
        assert result.issue.state == expected

    def test_disallowed_author_is_not_dropped_here(self):
        payload = _comment_payload({"user": {"login": "stranger"}, "author_association": "NONE"})

        result = _parse("issue_comment", payload)

        assert isinstance(result, TriggerEvent)
        assert result.trigger_comment is not None
        assert result.trigger_comment.author.association == "none"

    def test_missing_association_is_unknown(self):
        payload = _comment_payload({"user": {"login": "stranger"}})
        del payload["comment"]["author_association"]

        result = _parse("issue_comment", payload)

        assert isinstance(result, TriggerEvent)
        assert result.trigger_comment is not None
        assert result.trigger_comment.author.association == "unknown"


class TestEventIsAllowed:
    @pytest.mark.parametrize(
        "event,expected",
        [
            ("issues", True),
            ("issue_comment", True),
            # check_run was accepted historically but never produced a
            # TriggerKind — since dropped from the allowlist.
            # pull_request_review was scaffolded for a future PR-review
            # feedback loop but never wired to any handler — removed.
            ("pull_request_review", False),
            ("check_run", False),
            ("pull_request", False),
        ],
    )
    def test_allowlist(self, event, expected):
        assert event_is_allowed(event) is expected


def _adapter(secret: str = "s3cr3t") -> GitHubWebhookAdapter:
    return GitHubWebhookAdapter(
        instance_id="gh-test",
        secret=secret,
        identity=bot_identity_from_settings(
            _settings().providers["github"], _settings().agent_name
        ),
    )


def _headers(
    body: bytes, *, event: str = "issues", secret: str = "s3cr3t", **extra: str
) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "X-GitHub-Delivery": "delivery-1",
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": f"sha256={digest}",
    }
    headers.update(extra)
    return headers


class TestAdapter:
    def test_delivery_key_is_the_delivery_header(self):
        body = json.dumps(_payload("opened")).encode()

        assert _adapter().delivery_key(_headers(body), body) == "delivery-1"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_missing_delivery_header_is_rejected(self, value):
        body = b"{}"
        with pytest.raises(WebhookRejectedError) as exc:
            _adapter().delivery_key(_headers(body, **{"X-GitHub-Delivery": value}), body)

        assert exc.value.error == "missing X-GitHub-Delivery header"
        assert exc.value.mark_reason is None

    def test_valid_signature_passes(self):
        body = json.dumps(_payload("opened")).encode()

        _adapter().verify_signature(_headers(body), body)

    def test_bad_signature_is_rejected_without_burning_the_delivery(self):
        body = json.dumps(_payload("opened")).encode()
        with pytest.raises(WebhookRejectedError) as exc:
            _adapter().verify_signature(_headers(body, secret="wrong"), body)

        assert exc.value.error == "invalid signature"
        assert exc.value.mark_reason is None

    def test_empty_secret_rejects_everything(self):
        body = json.dumps(_payload("opened")).encode()
        with pytest.raises(WebhookRejectedError):
            _adapter(secret="").verify_signature(_headers(body, secret=""), body)

    def test_parse_event_normalizes_into_the_instance(self):
        body = json.dumps(_payload("opened")).encode()

        parsed = _adapter().parse_event(_headers(body), body)

        assert parsed.event == "issues"
        assert parsed.action == "opened"
        assert isinstance(parsed.result, TriggerEvent)
        assert parsed.result.delivery_key == "delivery-1"
        assert parsed.result.issue.provider_instance == "gh-test"

    def test_unsupported_event_is_ignored_with_no_repo(self):
        body = json.dumps(_payload("opened")).encode()

        parsed = _adapter().parse_event(_headers(body, event="pull_request"), body)

        assert parsed.event == "pull_request"
        assert parsed.action == "unsupported_event"
        assert parsed.result == Ignored("unsupported_event", None)

    def test_invalid_json_is_rejected_and_recorded(self):
        body = b"{not json"
        with pytest.raises(WebhookRejectedError) as exc:
            _adapter().parse_event(_headers(body), body)

        assert exc.value.error == "invalid JSON payload"
        assert exc.value.mark_reason == "invalid_json"

    @pytest.mark.parametrize(
        "body",
        [
            b"[1, 2]",
            json.dumps(_payload("opened", issue=_issue(number="nope"))).encode(),
        ],
    )
    def test_malformed_payload_is_rejected_and_recorded(self, body):
        with pytest.raises(WebhookRejectedError) as exc:
            _adapter().parse_event(_headers(body), body)

        assert exc.value.error == "invalid payload"
        assert exc.value.mark_reason == "invalid_payload"
