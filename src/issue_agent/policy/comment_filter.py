"""Ingestion-time author filter for issue comments (R2).

Decides whether an ``issue_comment.created`` webhook may become a paid run.
A rejected comment is dropped silently at the webhook handler — no task, no
GitHub comment, no LLM cost (C2, D5). This is a different layer from
``policy.guards.check_author_allowed``: that guard runs in the worker and
decides whether an already-created task may execute; this one decides whether
the webhook becomes a task at all (R2.4/D9).

The same predicate also filters which comments are worth spending context
window on when the run is built (R3.3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from issue_agent.config import Settings
    from issue_agent.core.models import Author


def comment_author_allowed(
    *,
    author: Author,
    issue_author_login: str | None,
    settings: Settings,
) -> bool:
    """True when a comment by ``author`` may trigger the agent.

    The agent's own comments never do. Otherwise any one condition is enough:

    * the filter is disabled (anyone may trigger a run);
    * the author opened the issue (matched by login, never by account type,
      so a bot that filed the issue keeps its follow-up channel for as long
      as the issue stays open — R2.1, user directive);
    * the author's canonical association is in the allowed set
      (``OWNER``/``MEMBER``/``COLLABORATOR`` by default, compared
      case-insensitively). An ``unknown`` association — a forge that doesn't
      report one — never matches; only the issue-author and allowlist checks
      can clear it (PRD §3.1);
    * the login is on the explicit allowlist (bots included — R2.6).
    """
    if author.is_self:
        return False
    if not settings.comments_author_filter_enabled:
        return True

    login = author.login.strip().lower()

    if (
        settings.comments_author_filter_allow_issue_author
        and login
        and login == (issue_author_login or "").strip().lower()
    ):
        return True

    allowed_associations = {
        a.strip().lower() for a in settings.comments_author_filter_allow_associations
    }
    if author.association != "unknown" and author.association in allowed_associations:
        return True

    allowlist = {a.strip().lower() for a in settings.comments_author_filter_allowlist}
    return bool(login and login in allowlist)
