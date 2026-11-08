from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from issue_agent.core.models import PRRef, PRState
from issue_agent.providers.base import PullRequestClient
from issue_agent.providers.github.client import GitHubClient

if TYPE_CHECKING:
    from github.PullRequest import PullRequest

    from issue_agent.providers.github.auth import GitHubAppAuth


class PRHeadMismatchError(RuntimeError):
    """A PR was found for the work branch but its head is a different ref.

    The run must not report it: a PR that is not on the work branch is some
    other issue's PR, and naming it strands this run's commits (issue #89)."""


def _find_pr_on_branch(
    client: GitHubClient, repo_full_name: str, branch_name: str
) -> PullRequest | None:
    existing = client.find_pr_by_branch(repo_full_name, branch_name)
    if existing is not None and existing.head.ref != branch_name:
        raise PRHeadMismatchError(
            f"PR #{existing.number} in {repo_full_name} has head "
            f"'{existing.head.ref}', not the work branch '{branch_name}' — "
            "refusing to report it as this run's PR"
        )
    return existing


def _poll_conflicts(
    client: GitHubClient,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    attempts: int,
    delay_seconds: float,
) -> bool | None:
    """Whether GitHub reports the PR as conflicting with its base, read
    once it has computed mergeability for ``head_sha`` (the commit just
    pushed — right after a push the API can still serve the previous
    head's answer, or ``None`` while it recomputes). ``None`` when that
    is still unknown after ``attempts`` reads, or on any API error: the
    caller only warns on a definite ``True``."""
    for attempt in range(attempts):
        try:
            pr = client.get_pull_request(repo_full_name, pr_number)
        except Exception:
            return None
        if pr.head.sha == head_sha and pr.mergeable is not None:
            return pr.mergeable is False
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return None


def _as_utc(ts: datetime | None) -> datetime | None:
    """PyGithub's timestamps are UTC; older versions hand them back naive."""
    if ts is None:
        return None
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def _pr_ref(pr: PullRequest) -> PRRef:
    return PRRef(number=pr.number, url=pr.html_url, head_ref=pr.head.ref)


class GitHubPullRequestClient(PullRequestClient):
    """``PullRequestClient`` for one target repo."""

    def __init__(
        self,
        auth: GitHubAppAuth,
        repo: str,
        *,
        label_agent_generated: str,
        conflict_attempts: int = 5,
        conflict_delay_seconds: float = 2.0,
    ) -> None:
        self._auth = auth
        self._repo_name = repo
        self._label_agent_generated = label_agent_generated
        self._conflict_attempts = conflict_attempts
        self._conflict_delay_seconds = conflict_delay_seconds

    def _client(self) -> GitHubClient:
        return GitHubClient(self._auth, self._auth.installation_for(self._repo_name))

    def find_open_pr(self, branch: str) -> PRRef | None:
        pr = _find_pr_on_branch(self._client(), self._repo_name, branch)
        return _pr_ref(pr) if pr is not None else None

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
        client = self._client()
        pr = client.create_pr(
            full_name=self._repo_name,
            title=title,
            body=body,
            head=head,
            base=base,
            draft=draft,
        )
        # GitHub labels PRs through the Issues API.
        client.get_repo(self._repo_name).get_issue(pr.number).add_to_labels(
            self._label_agent_generated, *labels
        )
        return _pr_ref(pr)

    def comment_on_pr(self, pr: PRRef, body: str) -> str:
        return self._client().create_comment(self._repo_name, pr.number, body).html_url

    def pr_has_conflicts(self, pr: PRRef, head_sha: str) -> bool | None:
        try:
            client = self._client()
        except Exception:
            return None
        return _poll_conflicts(
            client,
            self._repo_name,
            pr.number,
            head_sha,
            self._conflict_attempts,
            self._conflict_delay_seconds,
        )

    def get_pr_state(self, number: int) -> PRState:
        pr = self._client().get_pull_request(self._repo_name, number)
        if pr.merged:
            return PRState(state="merged", closed_at=_as_utc(pr.merged_at))
        if pr.state == "closed":
            return PRState(state="closed", closed_at=_as_utc(pr.closed_at))
        return PRState(state="open", closed_at=None)
