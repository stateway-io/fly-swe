"""Gitea's ``WebhookAdapter`` (PRD-Gitea §3.2, §4.2).

Everything that knows a Gitea webhook — the ``X-Gitea-*`` headers, the
bare-hex ``X-Gitea-Signature`` HMAC and the payload shape — lives here.
Gitea also sends ``X-GitHub-*``, ``X-Gogs-*`` and ``X-Hub-Signature*`` for
compatibility; they are ignored, since the provider is chosen by the
webhook path, not by guessing from headers.

**Delivery key.** Gitea's "Replay" sends a *new* ``X-Gitea-Delivery`` UUID
with a byte-identical body (measured on 1.27.3 and 28.4.0), so the UUID
cannot dedupe a replay. The key is ``sha256(event, event type, raw body)``
instead: computed without parsing (it runs before the signature check), and
two distinct events always differ in the body (``issue.updated_at``, the
labels, ``comment.id``). ``X-Gitea-Delivery`` is still required — a request
without it is not a Gitea delivery.

This module only normalizes; whether the event is worth a task is the
core's call (``policy/ingestion.py``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from issue_agent.core.models import Ignored, TriggerEvent, TriggerKind
from issue_agent.providers.base import ParsedDelivery, WebhookAdapter, WebhookRejectedError
from issue_agent.providers.gitea.normalize import (
    author_from_gitea,
    comment_from_gitea,
    is_pull,
    issue_ref_from_gitea,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_EVENT = "X-Gitea-Event"
_EVENT_TYPE = "X-Gitea-Event-Type"
_DELIVERY = "X-Gitea-Delivery"
_SIGNATURE = "X-Gitea-Signature"

# (X-Gitea-Event, action) -> kind. Label changes arrive as event "issues"
# (event type "issue_label") with the *current* label set, no diff.
_KINDS: dict[tuple[str, str], TriggerKind] = {
    ("issues", "opened"): TriggerKind.ISSUE_OPENED,
    ("issues", "label_updated"): TriggerKind.LABEL_ADDED,
    ("issues", "label_cleared"): TriggerKind.LABEL_ADDED,
    ("issues", "closed"): TriggerKind.ISSUE_CLOSED,
    ("issues", "reopened"): TriggerKind.ISSUE_REOPENED,
    ("issue_comment", "created"): TriggerKind.COMMENT_COMMAND,
}
_ALLOWED_EVENTS = frozenset(event for event, _ in _KINDS)


class GiteaWebhookAdapter(WebhookAdapter):
    def __init__(self, *, instance_id: str, secret: str, bot_login: str) -> None:
        self._instance_id = instance_id
        self._secret = secret
        self._bot_login = bot_login

    def delivery_key(self, headers: Mapping[str, str], body: bytes) -> str:
        if not _header(headers, _DELIVERY):
            raise WebhookRejectedError(
                f"missing {_DELIVERY} header", log_event="missing_delivery_header"
            )
        digest = hashlib.sha256()
        for part in (_header(headers, _EVENT), _header(headers, _EVENT_TYPE)):
            digest.update(part.encode())
            digest.update(b"\0")
        digest.update(body)
        return f"gitea-{digest.hexdigest()}"

    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> None:
        if not verify_webhook_signature(body, _header(headers, _SIGNATURE), self._secret):
            raise WebhookRejectedError("invalid signature", log_event="invalid_signature")

    def event_name(self, headers: Mapping[str, str]) -> str:
        """``X-Gitea-Event``, with the finer ``X-Gitea-Event-Type`` appended
        when it differs (``issues/issue_label``,
        ``issue_comment/pull_request_comment``) so the log shows both."""
        event, event_type = _header(headers, _EVENT), _header(headers, _EVENT_TYPE)
        return f"{event}/{event_type}" if event_type and event_type != event else event

    def parse_event(self, headers: Mapping[str, str], body: bytes) -> ParsedDelivery:
        name = self.event_name(headers)
        event = _header(headers, _EVENT)
        if event not in _ALLOWED_EVENTS:
            # push, pull_request, ... when the hook subscribes to more than
            # the issue events: valid, signed deliveries we don't act on.
            return ParsedDelivery(name, None, Ignored("unsupported_event", None))
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise WebhookRejectedError(
                "invalid JSON payload", log_event="invalid_json", mark_reason="invalid_json"
            ) from None
        invalid = WebhookRejectedError(
            "invalid payload", log_event="invalid_payload", mark_reason="invalid_payload"
        )
        if not (
            isinstance(payload, dict)
            and isinstance(payload.get("issue"), dict)
            and isinstance(payload.get("repository"), dict)
        ):
            raise invalid
        action = payload.get("action")
        action = action if isinstance(action, str) else None
        try:
            result = self._normalize(self.delivery_key(headers, body), event, action, payload)
        except ValidationError:
            raise invalid from None
        return ParsedDelivery(name, action, result)

    def _normalize(
        self, delivery_key: str, event: str, action: str | None, payload: dict[str, Any]
    ) -> TriggerEvent | Ignored:
        repo = payload["repository"].get("full_name")
        repo = repo if isinstance(repo, str) else ""
        kind = _KINDS.get((event, action or ""))
        if kind is None:
            return Ignored("unhandled_event_action", repo)

        issue = issue_ref_from_gitea(
            payload["issue"],
            repo=repo,
            instance_id=self._instance_id,
            bot_login=self._bot_login,
            # label_cleared still lists the labels from before the clear.
            labels=[] if action == "label_cleared" else None,
        )
        comment = payload.get("comment")
        trigger_comment = None
        if kind is TriggerKind.COMMENT_COMMAND:
            trigger_comment = comment_from_gitea(
                comment if isinstance(comment, dict) else {}, bot_login=self._bot_login
            )
        return TriggerEvent(
            kind=kind,
            target_kind="pr" if is_pull(payload) else "issue",
            issue=issue,
            actor=author_from_gitea(payload.get("sender"), bot_login=self._bot_login),
            trigger_comment=trigger_comment,
            delivery_key=delivery_key,
            # One token per instance: nothing to pick per delivery.
            credentials_ref=None,
        )


def verify_webhook_signature(payload: bytes, signature_header: str, secret: str) -> bool:
    """Timing-safe ``X-Gitea-Signature`` check: the bare hex HMAC-SHA256 of
    the raw body. Fails closed on an empty secret (an HMAC keyed with "" is
    forgeable by anyone) and on an empty signature (what Gitea sends from a
    hook with no secret)."""
    if not secret.strip() or not signature_header:
        return False
    try:
        expected = bytes.fromhex(signature_header)
    except ValueError:
        return False
    computed = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return hmac.compare_digest(computed, expected)


def _header(headers: Mapping[str, str], name: str) -> str:
    return (headers.get(name) or "").strip()
