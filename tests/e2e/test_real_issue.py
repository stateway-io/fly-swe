from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest

from issue_agent.providers.github.auth import GitHubAppAuth
from issue_agent.providers.github.client import GitHubClient

if TYPE_CHECKING:
    from github.IssueComment import IssueComment
    from github.Repository import Repository

POLL_INTERVAL = 3
POLL_TIMEOUT = 45
LABEL_QUESTION = "question"


def _get_repo(settings: Any, repo_full_name: str) -> Repository:
    auth = GitHubAppAuth(settings.providers["github"])
    owner, name = repo_full_name.split("/", 1)
    client = GitHubClient(auth, auth.resolve_installation_id(owner, name))
    return client.get_repo(repo_full_name)


def _ensure_label(repo: Repository, name: str, color: str = "d4c5f9") -> None:
    try:
        repo.get_label(name)
    except Exception:
        repo.create_label(name, color)


def _create_question_issue(repo: Repository, unique_id: int) -> Any:
    _ensure_label(repo, LABEL_QUESTION)
    return repo.create_issue(
        title=f"[E2E TEST {unique_id}] How do I deploy the Stateway engine?",
        body="I want to know how to deploy the Stateway engine in production.",
        labels=[LABEL_QUESTION],
    )


def _poll_for_bot_comment(
    repo: Repository,
    issue_number: int,
    *,
    timeout: int = POLL_TIMEOUT,
    interval: int = POLL_INTERVAL,
) -> IssueComment:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        issue = repo.get_issue(issue_number)
        for comment in issue.get_comments():
            user = comment.user
            if user and user.type == "Bot":
                return comment
        time.sleep(interval)
    pytest.fail(f"Bot comment not found on issue #{issue_number} after {timeout}s timeout")


@pytest.mark.e2e
def test_triage_comment_on_question_issue(e2e_settings: Any, e2e_test_repo: str) -> None:
    repo = _get_repo(e2e_settings, e2e_test_repo)
    unique_id = int(time.time())
    issue = _create_question_issue(repo, unique_id)

    try:
        comment = _poll_for_bot_comment(repo, issue.number)

        assert e2e_settings.agent_name in comment.body, (
            f"Comment should mention '{e2e_settings.agent_name}', got:\n{comment.body}"
        )
        assert "Obrigado pela pergunta" in comment.body, (
            f"Comment should thank for the question, got:\n{comment.body}"
        )
        assert issue.user.login in comment.body, (
            f"Comment should @mention the author @{issue.user.login}, got:\n{comment.body}"
        )
    finally:
        issue.edit(state="closed")
