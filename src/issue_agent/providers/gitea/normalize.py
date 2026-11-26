"""Gitea shape -> canonical model (PRD-Gitea §4.2).

The single place that knows how Gitea spells an issue, a comment and its
author. Shared by the webhook adapter (raw payloads) and the API client
(comment history), so both produce the same ``Author``.

Gitea reports no ``author_association``: every author is ``"unknown"``
(the worker may ask ``author_permission``), and "this service's own bot" is
matched on the configured ``bot_login`` alone — a ``user.type`` hint (Gitea
28 bot accounts) is not relied upon.
"""

from __future__ import annotations

from typing import Any, Literal

from issue_agent.core.models import Author, Comment, IssueRef


def author_from_gitea(user: Any, *, bot_login: str) -> Author:
    login = user.get("login") if isinstance(user, dict) else None
    login = login if isinstance(login, str) else ""
    return Author(
        login=login,
        association="unknown",
        # An unset bot_login matches nobody (health_check reports it).
        is_self=bool(login) and bool(bot_login) and login.lower() == bot_login.lower(),
    )


def issue_ref_from_gitea(
    issue: dict[str, Any],
    *,
    repo: str,
    instance_id: str,
    bot_login: str,
    labels: list[str] | None = None,
) -> IssueRef:
    """``labels`` overrides the payload's set — for ``label_cleared``, whose
    ``issue.labels`` still lists the labels from before the clear."""
    return IssueRef(
        provider_instance=instance_id,
        repo=repo,
        number=issue.get("number") or 0,
        global_id=str(issue["id"]) if issue.get("id") is not None else None,
        title=issue.get("title") or "",
        body=issue.get("body") or "",
        labels=label_names(issue) if labels is None else labels,
        author=author_from_gitea(issue.get("user"), bot_login=bot_login),
        url=issue.get("html_url") or "",
        state=issue_state(issue.get("state")),
    )


def comment_from_gitea(comment: dict[str, Any], *, bot_login: str) -> Comment:
    return Comment(
        id=str(comment.get("id", "")),
        author=author_from_gitea(comment.get("user"), bot_login=bot_login),
        body=comment.get("body") or "",
        created_at=comment.get("created_at") or None,
    )


def label_names(issue: dict[str, Any]) -> list[str]:
    raw = issue.get("labels") or []
    if not isinstance(raw, list):
        return []
    return [lb["name"] for lb in raw if isinstance(lb, dict) and isinstance(lb.get("name"), str)]


def is_pull(payload: dict[str, Any]) -> bool:
    """A comment on a PR: Gitea sets ``is_pull`` and ``issue.pull_request``
    (issues and PRs share one index per repo)."""
    issue = payload.get("issue")
    return payload.get("is_pull") is True or (
        isinstance(issue, dict) and bool(issue.get("pull_request"))
    )


def issue_state(raw: Any) -> Literal["open", "closed"] | None:
    state = raw.lower() if isinstance(raw, str) else None
    return state if state in ("open", "closed") else None  # type: ignore[return-value]
