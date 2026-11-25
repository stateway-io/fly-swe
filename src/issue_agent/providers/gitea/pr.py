"""Gitea's ``PullRequestClient`` (PRD-Gitea §4.4).

Draft PRs: Gitea has no draft flag; a PR is "work in progress" while its
title starts with a ``WIP_PREFIXES`` entry. The core applies the prefix
itself (``capabilities.draft_mode == "title_prefix"``), so ``create_pr``
takes the title as given and ignores ``draft``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from issue_agent.core.models import PRRef, PRState
from issue_agent.providers.base import PullRequestClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from issue_agent.providers.gitea.api import GiteaAPI


class GiteaPullRequestClient(PullRequestClient):
    def __init__(
        self,
        api: GiteaAPI,
        repo: str,
        *,
        label_agent_generated: str,
        conflict_reads: int = 3,
        conflict_max_reads: int = 10,
        conflict_interval_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api = api
        self._repo = repo
        self._label_agent_generated = label_agent_generated
        self._conflict_reads = conflict_reads
        self._conflict_max_reads = conflict_max_reads
        self._conflict_interval = conflict_interval_seconds
        self._sleep = sleep

    def find_open_pr(self, branch: str) -> PRRef | None:
        """The open PR from ``branch`` *of this repo*. Gitea's list has no
        head filter that works on every version, so filter client-side; a
        fork's branch of the same name (or a deleted fork, ``head.repo``
        null) is someone else's PR."""
        for pr in self._api.paginate(f"/repos/{self._repo}/pulls", params={"state": "open"}):
            head = _dict(_dict(pr).get("head"))
            if head.get("ref") == branch and _same_repo(
                _dict(head.get("repo")).get("full_name"), self._repo
            ):
                return _pr_ref(pr)
        return None

    def create_pr(
        self,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool,
        labels: list[str],
    ) -> PRRef:
        ids = self._api.label_ids(self._repo, [self._label_agent_generated, *labels])
        pr = self._api.post(
            f"/repos/{self._repo}/pulls",
            json={"head": head, "base": base, "title": title, "body": body, "labels": ids},
        )
        return _pr_ref(pr)

    def comment_on_pr(self, pr: PRRef, body: str) -> str:
        comment = self._api.post(
            f"/repos/{self._repo}/issues/{pr.number}/comments", json={"body": body}
        )
        return comment.get("html_url") or ""

    def pr_has_conflicts(self, pr: PRRef, head_sha: str) -> bool | None:
        """Gitea computes mergeability in the background and exposes no
        "checking" state: right after a push or creation, ``mergeable`` can
        read a stale value either way (measured: ``false`` for ~1.5 s on a
        clean PR, ``true`` for ~0.2 s after a conflicting push). So only
        ``conflict_reads`` consecutive agreeing reads on ``head_sha`` are a
        verdict; anything else — no agreement within ``conflict_max_reads``,
        an API error — is None, and the core checks locally instead."""
        streak: bool | None = None
        count = 0
        for attempt in range(self._conflict_max_reads):
            if attempt:
                self._sleep(self._conflict_interval)
            try:
                data = self._api.get(f"/repos/{self._repo}/pulls/{pr.number}")
            except Exception:
                return None
            mergeable = _dict(data).get("mergeable")
            if _dict(_dict(data).get("head")).get("sha") != head_sha or not isinstance(
                mergeable, bool
            ):
                streak, count = None, 0
                continue
            count = count + 1 if mergeable is streak else 1
            streak = mergeable
            if count >= self._conflict_reads:
                return streak is False
        return None

    def get_pr_state(self, number: int) -> PRState:
        pr = self._api.get(f"/repos/{self._repo}/pulls/{number}")
        if pr.get("merged"):
            return PRState(state="merged", closed_at=_utc(pr.get("merged_at")))
        if pr.get("state") == "closed":
            return PRState(state="closed", closed_at=_utc(pr.get("closed_at")))
        return PRState(state="open", closed_at=None)


def _pr_ref(pr: dict[str, Any]) -> PRRef:
    return PRRef(number=pr["number"], url=pr.get("html_url") or "", head_ref=pr["head"]["ref"])


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _same_repo(full_name: Any, repo: str) -> bool:
    return isinstance(full_name, str) and full_name.lower() == repo.lower()


def _utc(raw: Any) -> datetime | None:
    """Gitea's RFC 3339 timestamps (``...Z`` or ``...+02:00``) in UTC."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
