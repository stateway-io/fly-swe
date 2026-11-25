"""Gitea's ``ForgeClient``: issue operations on one repo (PRD-Gitea §4.3).

Cheap to build — the core asks the plugin for one on every use — because
the HTTP client and its caches live on the shared ``GiteaAPI``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from issue_agent.providers.base import ForgeClient
from issue_agent.providers.gitea.normalize import comment_from_gitea, issue_state

if TYPE_CHECKING:
    from issue_agent.core.models import Association, Comment
    from issue_agent.providers.gitea.api import GiteaAPI

# Gitea's /collaborators/{login}/permission → the core's association.
_ASSOCIATION: dict[str, Association] = {
    "owner": "owner",
    "admin": "collaborator",
    "write": "collaborator",
    "read": "none",
    "none": "none",
}


class GiteaForgeClient(ForgeClient):
    def __init__(
        self,
        api: GiteaAPI,
        repo: str,
        *,
        bot_login: str,
        author_permission: Literal["allowlist", "api"] = "allowlist",
    ) -> None:
        self._api = api
        self._repo = repo
        self._bot_login = bot_login
        self._author_permission = author_permission

    def get_issue_state(self, number: int) -> Literal["open", "closed"]:
        issue = self._api.get(f"/repos/{self._repo}/issues/{number}")
        state = issue_state(issue.get("state") if isinstance(issue, dict) else None)
        return "closed" if state == "closed" else "open"

    def list_comments(self, number: int, limit: int) -> list[Comment]:
        if limit <= 0:
            return []
        # No limit/page on this endpoint (measured): it returns every
        # comment, oldest first.
        comments = self._api.get(f"/repos/{self._repo}/issues/{number}/comments")
        comments = [c for c in comments or [] if isinstance(c, dict)]
        return [comment_from_gitea(c, bot_login=self._bot_login) for c in comments[-limit:]]

    def post_comment(self, number: int, body: str) -> str:
        comment = self._api.post(
            f"/repos/{self._repo}/issues/{number}/comments", json={"body": body}
        )
        return comment.get("html_url") or ""

    def get_default_branch(self) -> str:
        return self._api.get(f"/repos/{self._repo}")["default_branch"]

    def add_labels(self, number: int, labels: list[str]) -> None:
        """Apply existing labels by ID. A name the repo (or its org) doesn't
        have is logged and skipped — the service never creates labels in
        someone's repo."""
        ids = self._api.label_ids(self._repo, labels)
        if ids:
            self._api.post(f"/repos/{self._repo}/issues/{number}/labels", json={"labels": ids})

    def author_permission(self, login: str) -> Association:
        if self._author_permission != "api" or not login:
            return "unknown"
        permission = self._api.permission(self._repo, login)
        return _ASSOCIATION.get(permission or "", "unknown")
