from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from github.Issue import Issue
from github.IssueComment import IssueComment
from github.PullRequest import PullRequest
from github.Repository import Repository

from issue_agent.core.models import Author, Comment
from issue_agent.providers.github.client import GitHubClient
from issue_agent.providers.github.normalize import BotIdentity


def _fake_pr(number: int, head_ref: str) -> MagicMock:
    pr = MagicMock(spec=PullRequest)
    pr.number = number
    pr.head.ref = head_ref
    return pr


class _FakeRepo:
    """Replicates GitHub's documented `GET /repos/{o}/{r}/pulls` behaviour.

    A `head` without a `user:`/`org:` qualifier is NOT an error — the filter
    is silently dropped and every open PR comes back. Only `owner:ref`
    filters. Mocking `get_pulls` away is what let issue #89 through."""

    def __init__(self, pulls: list[MagicMock]) -> None:
        self._pulls = pulls

    def get_pulls(self, state: str, head: str) -> list[MagicMock]:
        if ":" not in head:
            return list(self._pulls)
        _, ref = head.split(":", 1)
        return [pr for pr in self._pulls if pr.head.ref == ref]


@pytest.fixture
def mock_github() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_repo() -> MagicMock:
    return MagicMock(spec=Repository)


@pytest.fixture
def mock_issue() -> MagicMock:
    return MagicMock(spec=Issue)


@pytest.fixture
def mock_pr() -> MagicMock:
    return MagicMock(spec=PullRequest)


@pytest.fixture
def mock_comment() -> MagicMock:
    return MagicMock(spec=IssueComment)


@pytest.fixture
def mock_auth(mock_github: MagicMock) -> MagicMock:
    auth = MagicMock()
    auth.get_client.return_value = mock_github
    return auth


@pytest.fixture
def client(mock_auth: MagicMock) -> GitHubClient:
    return GitHubClient(auth=mock_auth, installation_id=456)


class TestGitHubClient:
    def test_get_github(self, client: GitHubClient, mock_auth: MagicMock) -> None:
        gh = client.get_github()
        assert gh is mock_auth.get_client.return_value
        mock_auth.get_client.assert_called_once()

    def test_get_github_passes_installation_id_to_auth(
        self, client: GitHubClient, mock_auth: MagicMock
    ) -> None:
        client.get_github()
        mock_auth.get_client.assert_called_once_with(456)

    def test_different_installation_ids_are_stored_independently(
        self, mock_auth: MagicMock
    ) -> None:
        client_a = GitHubClient(auth=mock_auth, installation_id=111)
        client_b = GitHubClient(auth=mock_auth, installation_id=222)

        client_a.get_github()
        client_b.get_github()

        mock_auth.get_client.assert_any_call(111)
        mock_auth.get_client.assert_any_call(222)
        assert mock_auth.get_client.call_count == 2

    def test_get_repo(
        self, client: GitHubClient, mock_github: MagicMock, mock_repo: MagicMock
    ) -> None:
        mock_github.get_repo.return_value = mock_repo
        repo = client.get_repo("owner/test-repo")
        assert repo is mock_repo
        mock_github.get_repo.assert_called_once_with("owner/test-repo")

    def test_get_issue(
        self,
        client: GitHubClient,
        mock_github: MagicMock,
        mock_repo: MagicMock,
        mock_issue: MagicMock,
    ) -> None:
        mock_github.get_repo.return_value = mock_repo
        mock_repo.get_issue.return_value = mock_issue

        issue = client.get_issue("owner/test-repo", 42)

        assert issue is mock_issue
        mock_github.get_repo.assert_called_once_with("owner/test-repo")
        mock_repo.get_issue.assert_called_once_with(42)

    def test_get_issue_comments(
        self,
        client: GitHubClient,
        mock_github: MagicMock,
        mock_repo: MagicMock,
        mock_issue: MagicMock,
    ) -> None:
        c1 = MagicMock(spec=IssueComment)
        c2 = MagicMock(spec=IssueComment)
        mock_github.get_repo.return_value = mock_repo
        mock_repo.get_issue.return_value = mock_issue
        mock_issue.get_comments.return_value = [c1, c2]

        comments = client.get_issue_comments("owner/test-repo", 7)

        assert comments == [c1, c2]
        mock_issue.get_comments.assert_called_once()

    def test_list_issue_comments_normalizes_and_takes_most_recent(
        self,
        client: GitHubClient,
        mock_github: MagicMock,
        mock_repo: MagicMock,
        mock_issue: MagicMock,
    ) -> None:
        from datetime import datetime

        def _c(cid, login, ctype, assoc, body, via_app_id=None):
            m = MagicMock(spec=IssueComment)
            m.id = cid
            m.user = MagicMock()
            m.user.login = login
            m.user.type = ctype
            m.body = body
            m.created_at = datetime(2026, 9, 8, 12, cid, 0)
            m.raw_data = {
                "author_association": assoc,
                "performed_via_github_app": ({"id": via_app_id} if via_app_id else None),
            }
            return m

        mock_github.get_repo.return_value = mock_repo
        mock_repo.get_issue.return_value = mock_issue
        mock_issue.get_comments.return_value = [
            _c(1, "reporter", "User", "NONE", "first"),
            _c(2, "maint", "User", "MEMBER", "second"),
            _c(3, "issue-agent[bot]", "Bot", "NONE", "third", via_app_id=123),
        ]

        identity = BotIdentity(app_id=123, app_name="issue-agent")
        result = client.list_issue_comments("owner/test-repo", 7, limit=2, identity=identity)

        assert [c.id for c in result] == ["2", "3"]
        assert result[0] == Comment(
            id="2",
            author=Author(login="maint", association="member", is_self=False),
            body="second",
            created_at=datetime(2026, 9, 8, 12, 2, 0),
        )
        assert result[1].author.is_self is True
        assert result[1].author.association == "none"

    def test_find_pr_by_branch_found(
        self, client: GitHubClient, mock_github: MagicMock, mock_repo: MagicMock, mock_pr: MagicMock
    ) -> None:
        mock_github.get_repo.return_value = mock_repo
        mock_repo.get_pulls.return_value = [mock_pr]
        mock_pr.head.ref = "agent/issue-42-fix-typo"

        result = client.find_pr_by_branch("owner/test-repo", "agent/issue-42-fix-typo")

        assert result is mock_pr
        mock_repo.get_pulls.assert_called_once_with(
            state="open",
            head="owner:agent/issue-42-fix-typo",
        )

    def test_find_pr_by_branch_not_found(
        self, client: GitHubClient, mock_github: MagicMock, mock_repo: MagicMock
    ) -> None:
        mock_github.get_repo.return_value = mock_repo
        mock_repo.get_pulls.return_value = []

        result = client.find_pr_by_branch("owner/test-repo", "agent/nonexistent")

        assert result is None
        mock_repo.get_pulls.assert_called_once()

    def test_find_pr_by_branch_honours_the_real_head_filter_contract(
        self, client: GitHubClient, mock_github: MagicMock
    ) -> None:
        """Against the real API a bare `head` is silently ignored and every
        open PR comes back, so the first one — someone else's — was returned
        and reported as this run's PR (issue #89). _FakeRepo reproduces that
        contract instead of mocking it away."""
        other = _fake_pr(244, "dev-some-other-issue")
        ours = _fake_pr(271, "agent/issue-42-fix-typo")
        mock_github.get_repo.return_value = _FakeRepo([other, ours])

        result = client.find_pr_by_branch("owner/test-repo", "agent/issue-42-fix-typo")

        assert result is ours

    def test_find_pr_by_branch_returns_none_when_only_unrelated_prs_are_open(
        self, client: GitHubClient, mock_github: MagicMock
    ) -> None:
        """The branch has no PR: the run must learn that and open one, not
        inherit the unrelated PR the unfiltered list starts with."""
        mock_github.get_repo.return_value = _FakeRepo([_fake_pr(244, "dev-some-other-issue")])

        result = client.find_pr_by_branch("owner/test-repo", "agent/issue-42-fix-typo")

        assert result is None

    def test_find_pr_by_branch_rejects_a_mismatched_head_from_the_filter(
        self, client: GitHubClient, mock_github: MagicMock, mock_repo: MagicMock
    ) -> None:
        """Backstop: even if the server-side filter hands back something that
        is not on the branch, it never reaches the caller."""
        mock_github.get_repo.return_value = mock_repo
        mock_repo.get_pulls.return_value = [_fake_pr(244, "dev-some-other-issue")]

        assert client.find_pr_by_branch("owner/test-repo", "agent/issue-42-fix-typo") is None

    def test_get_pull_request(
        self, client: GitHubClient, mock_github: MagicMock, mock_repo: MagicMock, mock_pr: MagicMock
    ) -> None:
        mock_github.get_repo.return_value = mock_repo
        mock_repo.get_pull.return_value = mock_pr

        pr = client.get_pull_request("owner/test-repo", 99)

        assert pr is mock_pr
        mock_repo.get_pull.assert_called_once_with(99)

    def test_create_comment(
        self,
        client: GitHubClient,
        mock_github: MagicMock,
        mock_repo: MagicMock,
        mock_issue: MagicMock,
        mock_comment: MagicMock,
    ) -> None:
        mock_github.get_repo.return_value = mock_repo
        mock_repo.get_issue.return_value = mock_issue
        mock_issue.create_comment.return_value = mock_comment

        comment = client.create_comment("owner/test-repo", 10, "Hello from agent")

        assert comment is mock_comment
        mock_issue.create_comment.assert_called_once_with("Hello from agent")

    def test_create_pr(
        self, client: GitHubClient, mock_github: MagicMock, mock_repo: MagicMock, mock_pr: MagicMock
    ) -> None:
        mock_github.get_repo.return_value = mock_repo
        mock_repo.create_pull.return_value = mock_pr

        pr = client.create_pr(
            full_name="owner/test-repo",
            title="[agent] fix(#42): typo in README",
            body="## Summary\n\nFixed a typo.",
            head="agent/issue-42-fix-typo",
            base="main",
        )

        assert pr is mock_pr
        mock_repo.create_pull.assert_called_once_with(
            title="[agent] fix(#42): typo in README",
            body="## Summary\n\nFixed a typo.",
            head="agent/issue-42-fix-typo",
            base="main",
        )

    def test_create_pr_with_extra_kwargs(
        self, client: GitHubClient, mock_github: MagicMock, mock_repo: MagicMock, mock_pr: MagicMock
    ) -> None:
        mock_github.get_repo.return_value = mock_repo
        mock_repo.create_pull.return_value = mock_pr

        pr = client.create_pr(
            full_name="owner/test-repo",
            title="title",
            body="body",
            head="head",
            base="base",
            maintainer_can_modify=True,
            draft=True,
        )

        assert pr is mock_pr
        mock_repo.create_pull.assert_called_once_with(
            title="title",
            body="body",
            head="head",
            base="base",
            maintainer_can_modify=True,
            draft=True,
        )

    def test_get_github_delegates_to_auth(self, client: GitHubClient, mock_auth: MagicMock) -> None:
        gh = client.get_github()
        assert gh is mock_auth.get_client.return_value

    def test_get_repo_passes_correct_args(
        self, client: GitHubClient, mock_github: MagicMock
    ) -> None:
        client.get_repo("a/b")
        mock_github.get_repo.assert_called_once_with("a/b")
