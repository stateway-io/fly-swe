from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from issue_agent.policy.comment_filter import comment_author_allowed

if TYPE_CHECKING:
    from collections.abc import Callable

    from issue_agent.config import Settings
    from issue_agent.core.models import Association
    from issue_agent.queue.models import Task
    from issue_agent.queue.store import SQLiteStore


def validate_task_payload(task: Task) -> tuple[bool, str]:
    """The task's canonical trigger exists and names an issue — every
    consumer after this guard relies on it."""
    trigger = task.trigger
    if trigger is None:
        return False, "invalid_trigger"

    if not trigger.issue.number:
        return False, "trigger_missing_issue_number"

    if not trigger.issue.repo:
        return False, "trigger_missing_repo"

    return True, ""


def check_repo_allowed(task: Task, settings: Settings) -> tuple[bool, str]:
    if task.repo in settings.allowed_repos:
        return True, ""
    return False, f"repo_not_allowed: {task.repo}"


def check_author_allowed(task: Task, settings: Settings) -> tuple[bool, str]:
    if not settings.allowed_authors:
        return True, ""

    trigger = task.trigger
    if trigger is None:
        return False, "invalid_trigger"

    actor = trigger.actor.login
    if not actor:
        return False, "trigger_missing_actor"

    if actor in settings.allowed_authors:
        return True, ""

    return False, f"author_not_allowed: {actor}"


def check_comment_author_allowed(
    task: Task,
    settings: Settings,
    author_permission: Callable[[str], Association],
) -> tuple[bool, str]:
    """Re-apply the comment-author filter the parser deferred (PRD §3.1).

    The parser drops a disallowed ``comment_command`` at ingestion, but only
    when the forge reported the author's association; an ``unknown`` one is
    enqueued so the decision can be made here, before any LLM spend, with
    the association ``author_permission`` (the forge's
    ``ForgeClient.author_permission``) resolves for the login.
    """
    trigger = task.trigger
    if trigger is None or trigger.trigger_comment is None:
        return True, ""
    author = trigger.trigger_comment.author
    if author.association != "unknown":
        return True, ""
    try:
        association = author_permission(author.login)
    except Exception:
        # Fail closed: the issue-author and allowlist checks still apply.
        association = "unknown"
    author = author.model_copy(update={"association": association})
    if comment_author_allowed(
        author=author,
        issue_author_login=trigger.issue.author.login,
        settings=settings,
    ):
        return True, ""
    return False, "comment_author_not_allowed"


def check_daily_budget(
    store: SQLiteStore, settings: Settings, scope: str
) -> tuple[bool, str]:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    budget = store.get_daily_budget(today, scope)
    if budget.total_cost_usd >= settings.max_daily_budget_usd:
        return (
            False,
            f"daily_budget_exceeded[{scope}]: {budget.total_cost_usd:.2f}/{settings.max_daily_budget_usd:.2f}",
        )
    return True, ""
