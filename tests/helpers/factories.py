"""Shared builders for the canonical domain model (PRD §3.1).

Plain functions with keyword overrides, like the per-file ``_make_task``
helpers, so any test tier can build a ``TriggerEvent`` without going through
a GitHub-shaped payload.
"""

from __future__ import annotations

from typing import Any

from issue_agent.core.models import Author, Comment, IssueRef, TriggerEvent, TriggerKind
from issue_agent.queue.models import Task


def make_author(**overrides: Any) -> Author:
    fields: dict[str, Any] = {"login": "test-user", "association": "none"}
    fields.update(overrides)
    return Author(**fields)


def make_comment(**overrides: Any) -> Comment:
    fields: dict[str, Any] = {
        "id": "1001",
        "author": make_author(login="maintainer", association="member"),
        "body": "please fix",
        "created_at": "2026-01-01T00:00:00Z",
    }
    fields.update(overrides)
    return Comment(**fields)


def make_issue(**overrides: Any) -> IssueRef:
    fields: dict[str, Any] = {
        "provider_instance": "github",
        "repo": "stateway-io/stateway-issues",
        "number": 42,
        "title": "Test issue",
        "body": "Something is broken",
        "labels": ["bug"],
        "author": make_author(),
        "url": "https://github.com/stateway-io/stateway-issues/issues/42",
    }
    fields.update(overrides)
    return IssueRef(**fields)


def make_trigger_event(
    kind: TriggerKind = TriggerKind.ISSUE_OPENED, **overrides: Any
) -> TriggerEvent:
    """A valid ``TriggerEvent``; ``comment_command`` gets a trigger comment
    and its author as the actor unless overridden."""
    fields: dict[str, Any] = {
        "kind": kind,
        "issue": make_issue(),
        "delivery_key": "delivery-1",
        "credentials_ref": "12345",
    }
    if kind is TriggerKind.COMMENT_COMMAND:
        fields["trigger_comment"] = make_comment()
    fields.update(overrides)
    if "actor" not in fields:
        comment = fields.get("trigger_comment")
        fields["actor"] = comment.author if comment else fields["issue"].author
    return TriggerEvent(**fields)


def make_task(trigger: TriggerEvent | None = None, **overrides: Any) -> Task:
    """A pending ``Task`` whose ``trigger_json`` is ``trigger`` (default:
    ``make_trigger_event()`` for the task's ``trigger_kind``), with the
    ``repo``/``issue_number`` columns mirroring the trigger's issue.

    Pass ``trigger_json=`` explicitly to store something that isn't a valid
    event (e.g. ``None`` or garbage) for the guard tests.
    """
    kind = overrides.get("trigger_kind", TriggerKind.ISSUE_OPENED)
    if trigger is None:
        trigger = make_trigger_event(kind)
    fields: dict[str, Any] = {
        "delivery_id": trigger.delivery_key,
        "repo": trigger.issue.repo,
        "issue_number": trigger.issue.number,
        "trigger_kind": trigger.kind,
        "target_repo": "stateway-io/stateway-agent-fixtures",
        "installation_id": 12345,
        "trigger_json": trigger.model_dump_json(),
        "provider_instance": trigger.issue.provider_instance,
        "credentials_ref": trigger.credentials_ref,
    }
    fields.update(overrides)
    return Task(**fields)
