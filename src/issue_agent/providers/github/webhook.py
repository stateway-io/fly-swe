"""GitHub's ``WebhookAdapter`` (PRD §3.2, §3.4).

Everything that knows a GitHub webhook — the ``X-GitHub-*`` headers, the
``X-Hub-Signature-256`` HMAC and the payload shape — lives here: downstream
of ``parse_event`` the core only sees ``TriggerEvent``. This module only
normalizes; whether the event is worth a task is the core's call
(``policy/ingestion.py``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError

from issue_agent.core.models import Comment, Ignored, IssueRef, TriggerEvent, TriggerKind
from issue_agent.providers.base import ParsedDelivery, WebhookAdapter, WebhookRejectedError
from issue_agent.providers.github.normalize import author_from_github

if TYPE_CHECKING:
    from collections.abc import Mapping

    from issue_agent.providers.github.normalize import BotIdentity


class GitHubWebhookAdapter(WebhookAdapter):
    def __init__(self, *, instance_id: str, secret: str, identity: BotIdentity) -> None:
        self._instance_id = instance_id
        self._secret = secret
        self._identity = identity

    def delivery_key(self, headers: Mapping[str, str], body: bytes) -> str:
        delivery_id = (headers.get("X-GitHub-Delivery") or "").strip()
        if not delivery_id:
            raise WebhookRejectedError(
                "missing X-GitHub-Delivery header", log_event="missing_delivery_header"
            )
        return delivery_id

    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> None:
        signature = (headers.get("X-Hub-Signature-256") or "").strip()
        if not verify_webhook_signature(body, signature, self._secret):
            raise WebhookRejectedError("invalid signature", log_event="invalid_signature")

    def event_name(self, headers: Mapping[str, str]) -> str:
        return (headers.get("X-GitHub-Event") or "").strip()

    def parse_event(self, headers: Mapping[str, str], body: bytes) -> ParsedDelivery:
        event = self.event_name(headers)
        if not event_is_allowed(event):
            # A merged PR also fires pull_request/push/check events when the
            # App subscribes to more than issues + issue_comment. Those are
            # valid, signed deliveries we simply don't act on.
            return ParsedDelivery(
                event, "unsupported_event", Ignored("unsupported_event", None)
            )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise WebhookRejectedError(
                "invalid JSON payload", log_event="invalid_json", mark_reason="invalid_json"
            ) from None
        invalid = WebhookRejectedError(
            "invalid payload", log_event="invalid_payload", mark_reason="invalid_payload"
        )
        if not isinstance(payload, dict):
            raise invalid
        try:
            result = normalize_payload(
                self.delivery_key(headers, body),
                event,
                payload,
                instance_id=self._instance_id,
                identity=self._identity,
            )
        except ValidationError:
            raise invalid from None
        return ParsedDelivery(event, event_action(payload), result)


def verify_webhook_signature(payload: bytes, signature_header: str, secret: str) -> bool:
    """Timing-safe ``X-Hub-Signature-256`` check. Fails closed on an empty
    secret: an HMAC keyed with "" is forgeable by anyone."""
    if not secret.strip():
        return False
    expected = _parse_signature_header(signature_header)
    if expected is None:
        return False

    computed = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).digest()

    return hmac.compare_digest(computed, expected)


def _parse_signature_header(header: str) -> bytes | None:
    if not header:
        return None
    if not header.startswith("sha256="):
        return None
    hex_digest = header.removeprefix("sha256=")
    if not hex_digest:
        return None
    try:
        return bytes.fromhex(hex_digest)
    except ValueError:
        return None


_ALLOWED_EVENTS = {
    "issues",
    "issue_comment",
}


def event_is_allowed(event: str) -> bool:
    return event in _ALLOWED_EVENTS


def event_action(payload: dict[str, Any]) -> str | None:
    return payload.get("action")


def _infer_trigger_kind(event: str, action: str | None) -> TriggerKind | None:
    if event == "issues" and action == "opened":
        return TriggerKind.ISSUE_OPENED
    if event == "issues" and action == "labeled":
        return TriggerKind.LABEL_ADDED
    if event == "issue_comment" and action == "created":
        return TriggerKind.COMMENT_COMMAND
    if event == "issues" and action == "closed":
        return TriggerKind.ISSUE_CLOSED
    if event == "issues" and action == "reopened":
        return TriggerKind.ISSUE_REOPENED
    return None


def normalize_payload(
    delivery_id: str,
    event: str,
    payload: dict[str, Any],
    *,
    instance_id: str,
    identity: BotIdentity,
) -> TriggerEvent | Ignored:
    """Normalize a GitHub ``issues``/``issue_comment`` payload.

    Returns ``Ignored`` for actions the agent doesn't handle.
    """
    issue = payload.get("issue") or {}
    repo: str = (payload.get("repository") or {}).get("full_name", "")

    kind = _infer_trigger_kind(event, event_action(payload))
    if kind is None:
        return Ignored("unhandled_event_action", repo)

    issue_author = author_from_github(
        issue.get("user"),
        association=issue.get("author_association"),
        performed_via_github_app_id=None,
        identity=identity,
    )
    raw_labels: list[dict[str, Any]] = issue.get("labels") or []
    issue_ref = IssueRef(
        provider_instance=instance_id,
        repo=repo,
        number=issue.get("number", 0),
        global_id=str(issue["id"]) if issue.get("id") is not None else None,
        title=issue.get("title", "") or "",
        body=issue.get("body", "") or "",
        labels=[
            lb["name"]
            for lb in raw_labels
            if isinstance(lb, dict) and isinstance(lb.get("name"), str)
        ],
        author=issue_author,
        url=issue.get("html_url", "") or "",
        state=_issue_state(issue.get("state")),
    )

    trigger_comment: Comment | None = None
    if kind is TriggerKind.COMMENT_COMMAND:
        comment = payload.get("comment") or {}
        trigger_comment = Comment(
            id=str(comment.get("id", "")),
            author=author_from_github(
                comment.get("user"),
                association=comment.get("author_association"),
                performed_via_github_app_id=(
                    comment.get("performed_via_github_app") or {}
                ).get("id"),
                identity=identity,
            ),
            body=comment.get("body", "") or "",
            created_at=comment.get("created_at"),
        )

    installation_id = (payload.get("installation") or {}).get("id")
    return TriggerEvent(
        kind=kind,
        # GitHub treats PRs as a superset of issues: issue_comment also fires
        # for top-level comments on a PR's Conversation tab, and the payload
        # then carries an ``issue.pull_request`` key.
        target_kind="pr" if "pull_request" in issue else "issue",
        issue=issue_ref,
        actor=author_from_github(
            payload.get("sender"),
            association=None,
            performed_via_github_app_id=None,
            identity=identity,
        ),
        trigger_comment=trigger_comment,
        delivery_key=delivery_id,
        credentials_ref=str(installation_id) if installation_id is not None else None,
    )


def _issue_state(raw: Any) -> Literal["open", "closed"] | None:
    state = raw.lower() if isinstance(raw, str) else None
    return state if state in ("open", "closed") else None  # type: ignore[return-value]
