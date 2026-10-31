"""Core ingestion filters over the canonical ``TriggerEvent`` (PRD §3.1).

The provider's webhook adapter only normalizes; whether a well-formed event
is worth a task is decided here, once, for every forge. Every check is on
canonical fields and none makes a network call, so the webhook handler
stays fast.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from issue_agent.core.models import TriggerKind
from issue_agent.policy.comment_filter import comment_author_allowed
from issue_agent.policy.label_router import labels_can_trigger

if TYPE_CHECKING:
    from issue_agent.config import Settings
    from issue_agent.core.models import TriggerEvent


def ingestion_filter(trigger: TriggerEvent, settings: Settings) -> str | None:
    """The reason to drop ``trigger`` at the webhook, or None to keep it."""
    if trigger.kind is TriggerKind.LABEL_ADDED and not labels_can_trigger(
        trigger.issue.labels
    ):
        return "no_trigger_label"
    if trigger.trigger_comment is not None:
        return _filter_comment(trigger, settings)
    return None


def _filter_comment(trigger: TriggerEvent, settings: Settings) -> str | None:
    """Ingestion filters for ``comment_command``, in order."""
    assert trigger.trigger_comment is not None
    if trigger.target_kind == "pr":
        # Never let a PR Conversation comment enqueue an issue-fix task,
        # regardless of whether repos.issues == repos.target. Reviews on the
        # agent's own PRs are a distinct, not-yet-built feature (a future "PR
        # review feedback loop" would consume a different trigger than this
        # one) — see the trigger-conditions doc for the current state.
        return "comment_on_pull_request"
    author = trigger.trigger_comment.author
    if author.is_self:
        return "own_bot_comment"
    if trigger.issue.state == "closed":
        return "issue_closed"
    if author.association != "unknown" and not comment_author_allowed(
        author=author,
        issue_author_login=trigger.issue.author.login,
        settings=settings,
    ):
        # Silent drop (R2.3, D5): no task, no GitHub comment, no cost. An
        # unknown association can't be judged here without a network call;
        # the worker re-checks it before spending anything (PRD §3.1).
        return "comment_author_not_allowed"
    return None
