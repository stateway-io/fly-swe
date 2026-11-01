from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

import pytest

from issue_agent.providers.github.auth import GitHubAppAuth
from issue_agent.providers.github.client import GitHubClient

if TYPE_CHECKING:
    from github.IssueComment import IssueComment
    from github.PullRequest import PullRequest
    from github.Repository import Repository

POLL_INTERVAL = 5
POLL_TIMEOUT = 180
LABEL_DOCS = "docs"

# Issue to be created — the intentional typo in stateway-agent-fixtures
# is "Statewa" instead of "Stateway". The agent is expected to fix it.
ISSUE_TITLE = "[E2E TEST] Fix typo: 'Statewa' should be 'Stateway' in README"
ISSUE_BODY = (
    "The README says 'Statewa' instead of 'Stateway'. Please fix this typo across all files."
)


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


def _create_docs_issue(repo: Repository, unique_id: int) -> Any:
    _ensure_label(repo, LABEL_DOCS)
    return repo.create_issue(
        title=f"{ISSUE_TITLE} ({unique_id})",
        body=ISSUE_BODY,
        labels=[LABEL_DOCS],
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


def _extract_pr_number(comment_body: str) -> int:
    match = re.search(r"#(\d+)", comment_body)
    if match:
        return int(match.group(1))
    msg = f"Could not extract PR number from comment:\n{comment_body}"
    raise ValueError(msg)


def _get_target_repo_and_client(
    settings: Any,
    issue_repo: str,
) -> tuple[Repository, GitHubClient]:
    auth = GitHubAppAuth(settings.providers["github"])
    route = settings.route_for("github", issue_repo)
    owner, name = route.target_repo.split("/", 1)
    client = GitHubClient(auth, auth.resolve_installation_id(owner, name))
    repo = client.get_repo(route.target_repo)
    return repo, client


def _close_pr_and_delete_branch(repo: Repository, pr: PullRequest) -> None:
    pr.edit(state="closed")
    try:
        ref = repo.get_git_ref(f"heads/{pr.head.ref}")
        ref.delete()
    except Exception:
        pass


@pytest.mark.e2e
def test_full_agent_fix_flow(e2e_settings: Any, e2e_test_repo: str) -> None:
    """Creates a real issue with the 'docs' label, waits for the bot to open
    a PR in the stateway-io/stateway-agent-fixtures fixture repository, and
    validates that the fix was applied (typo 'Statewa' → 'Stateway')."""
    issues_repo = _get_repo(e2e_settings, e2e_test_repo)
    unique_id = int(time.time())
    issue = _create_docs_issue(issues_repo, unique_id)

    pr: PullRequest | None = None

    try:
        comment = _poll_for_bot_comment(issues_repo, issue.number)

        assert "opened a PR" in comment.body, (
            f"Comment should mention 'opened a PR', got:\n{comment.body}"
        )
        assert e2e_settings.agent_name in comment.body, (
            f"Comment should mention '{e2e_settings.agent_name}', got:\n{comment.body}"
        )

        pr_number = _extract_pr_number(comment.body)
        target_repo, client = _get_target_repo_and_client(e2e_settings, e2e_test_repo)
        pr = target_repo.get_pull(pr_number)

        assert pr.state == "open", f"PR #{pr_number} should be open, got state={pr.state}"

        branch_name = pr.head.ref
        assert branch_name.startswith("agent/issue-"), (
            f"Branch name should start with 'agent/issue-', got '{branch_name}'"
        )

        pr_issue_number_match = re.search(r"fix\(#(\d+)\)", pr.title)
        assert pr_issue_number_match is not None, (
            f"PR title should contain fix(#N), got: {pr.title}"
        )
        pr_issue_ref = int(pr_issue_number_match.group(1))
        assert pr_issue_ref == issue.number, (
            f"PR title references issue #{pr_issue_ref}, but test issue is #{issue.number}"
        )

        # Verify the diff contains the typo fix
        files = pr.get_files()
        readme_found = False
        typo_fixed = False
        for f in files:
            if f.filename == "README.md":
                readme_found = True
                patch = f.patch or ""
                if "Stateway" in patch and "Statewa" not in patch.replace("Stateway", "", 1):
                    typo_fixed = True
                break
        assert readme_found, "PR should modify README.md"
        assert typo_fixed, "PR should fix 'Statewa' → 'Stateway' in README.md"

    finally:
        issue.edit(state="closed")
        if pr is not None:
            _close_pr_and_delete_branch(target_repo, pr)
