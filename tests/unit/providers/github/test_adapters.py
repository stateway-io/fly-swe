"""The GitHub implementations of the outbound ports, over a mocked PyGithub.

The PyGithub call shapes themselves are pinned by ``test_client.py`` and
``test_pr_ops.py``; this file checks what each port method returns and that
every call authenticates as the installation of the repo it was built for.
"""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import MagicMock

import pytest
from github.PullRequest import PullRequest

from issue_agent.core.models import Comment, PRRef
from issue_agent.providers.github.client import GitHubForgeClient
from issue_agent.providers.github.normalize import BotIdentity
from issue_agent.providers.github.plugin import GitHubProviderPlugin
from issue_agent.providers.github.pr import (
    GitHubPullRequestClient,
    PRHeadMismatchError,
)
from issue_agent.providers.github.transport import GitHubTransport

IDENTITY = BotIdentity(app_id=3735121, app_name="Stateway Issue Agent")
REPO = "org/target"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----"


@pytest.fixture
def github() -> MagicMock:
    return MagicMock()


@pytest.fixture
def auth(github: MagicMock) -> MagicMock:
    auth = MagicMock()
    auth.installation_for.return_value = 42
    auth.get_client.return_value = github
    auth.token_for.return_value = "ghs_token"
    return auth


def _gh_pr(number: int, head_ref: str, **attrs: Any) -> MagicMock:
    pr = MagicMock(spec=PullRequest)
    pr.number = number
    pr.html_url = f"https://github.com/{REPO}/pull/{number}"
    pr.head.ref = head_ref
    for name, value in attrs.items():
        setattr(pr, name, value)
    return pr


class TestForgeClient:
    @pytest.fixture
    def forge(self, auth: MagicMock) -> GitHubForgeClient:
        return GitHubForgeClient(auth, REPO, IDENTITY)

    def test_authenticates_as_the_repos_installation(
        self, forge: GitHubForgeClient, auth: MagicMock, github: MagicMock
    ) -> None:
        forge.get_default_branch()

        auth.installation_for.assert_called_with(REPO)
        auth.get_client.assert_called_with(42)
        github.get_repo.assert_called_with(REPO)

    @pytest.mark.parametrize(
        ("raw", "expected"), [("open", "open"), ("closed", "closed"), ("CLOSED", "closed")]
    )
    def test_issue_state(
        self, forge: GitHubForgeClient, github: MagicMock, raw: str, expected: str
    ) -> None:
        github.get_repo.return_value.get_issue.return_value.state = raw

        assert forge.get_issue_state(7) == expected
        github.get_repo.return_value.get_issue.assert_called_with(7)

    def test_post_comment_returns_its_url(
        self, forge: GitHubForgeClient, github: MagicMock
    ) -> None:
        issue = github.get_repo.return_value.get_issue.return_value
        issue.create_comment.return_value.html_url = "https://github.com/c/1"

        assert forge.post_comment(7, "hi") == "https://github.com/c/1"
        issue.create_comment.assert_called_once_with("hi")

    def test_default_branch(self, forge: GitHubForgeClient, github: MagicMock) -> None:
        github.get_repo.return_value.default_branch = "develop"

        assert forge.get_default_branch() == "develop"

    def test_add_labels(self, forge: GitHubForgeClient, github: MagicMock) -> None:
        forge.add_labels(7, ["a", "b"])

        github.get_repo.return_value.get_issue.return_value.add_to_labels.assert_called_once_with(
            "a", "b"
        )

    def test_list_comments_marks_own_comments(
        self, forge: GitHubForgeClient, github: MagicMock
    ) -> None:
        mine = MagicMock(
            id=1,
            body="mine",
            created_at=None,
            raw_data={"author_association": "NONE", "performed_via_github_app": {"id": 3735121}},
        )
        mine.user.login, mine.user.type = "stateway-issue-agent[bot]", "Bot"
        theirs = MagicMock(
            id=2, body="theirs", created_at=None, raw_data={"author_association": "MEMBER"}
        )
        theirs.user.login, theirs.user.type = "maintainer", "User"
        github.get_repo.return_value.get_issue.return_value.get_comments.return_value = [
            mine,
            theirs,
        ]

        comments = forge.list_comments(7, 10)

        assert all(isinstance(c, Comment) for c in comments)
        assert [(c.body, c.author.is_self) for c in comments] == [
            ("mine", True),
            ("theirs", False),
        ]
        assert comments[1].author.association == "member"

    def test_author_permission_is_unknown(self, forge: GitHubForgeClient) -> None:
        # GitHub's webhooks always carry the association; nothing to look up.
        assert forge.author_permission("anyone") == "unknown"


class TestPullRequestClient:
    @pytest.fixture
    def pulls(self, auth: MagicMock) -> GitHubPullRequestClient:
        return GitHubPullRequestClient(
            auth, REPO, label_agent_generated="agent-generated", conflict_delay_seconds=0
        )

    def test_find_open_pr(self, pulls: GitHubPullRequestClient, github: MagicMock) -> None:
        github.get_repo.return_value.get_pulls.return_value = [_gh_pr(5, "agent/issue-1")]

        assert pulls.find_open_pr("agent/issue-1") == PRRef(
            number=5, url=f"https://github.com/{REPO}/pull/5", head_ref="agent/issue-1"
        )
        github.get_repo.return_value.get_pulls.assert_called_once_with(
            state="open", head="org:agent/issue-1"
        )

    def test_find_open_pr_none(self, pulls: GitHubPullRequestClient, github: MagicMock) -> None:
        github.get_repo.return_value.get_pulls.return_value = []

        assert pulls.find_open_pr("agent/issue-1") is None

    def test_find_open_pr_never_reports_another_branch(
        self, pulls: GitHubPullRequestClient, github: MagicMock
    ) -> None:
        # The `head.ref` backstop in find_pr_by_branch already filters it; a
        # mismatch reaching the port would be the #89 class of error.
        github.get_repo.return_value.get_pulls.return_value = [_gh_pr(5, "other")]

        assert pulls.find_open_pr("agent/issue-1") is None

    def test_create_pr_labels_through_the_issues_api(
        self, pulls: GitHubPullRequestClient, github: MagicMock
    ) -> None:
        repo = github.get_repo.return_value
        repo.create_pull.return_value = _gh_pr(9, "agent/issue-1")

        pr = pulls.create_pr(
            head="agent/issue-1",
            base="main",
            title="t",
            body="b",
            draft=True,
            labels=["bug"],
        )

        assert pr.number == 9
        assert pr.url == f"https://github.com/{REPO}/pull/9"
        repo.create_pull.assert_called_once_with(
            title="t", body="b", head="agent/issue-1", base="main", draft=True
        )
        repo.get_issue.assert_called_with(9)
        repo.get_issue.return_value.add_to_labels.assert_called_once_with("agent-generated", "bug")

    def test_comment_on_pr(self, pulls: GitHubPullRequestClient, github: MagicMock) -> None:
        issue = github.get_repo.return_value.get_issue.return_value
        issue.create_comment.return_value.html_url = "https://github.com/c/2"
        pr = PRRef(number=9, url="u", head_ref="h")

        assert pulls.comment_on_pr(pr, "update") == "https://github.com/c/2"
        github.get_repo.return_value.get_issue.assert_called_with(9)

    @pytest.mark.parametrize(("mergeable", "expected"), [(False, True), (True, False)])
    def test_pr_has_conflicts(
        self,
        pulls: GitHubPullRequestClient,
        github: MagicMock,
        mergeable: bool,
        expected: bool,
    ) -> None:
        gh_pr = _gh_pr(9, "h", mergeable=mergeable)
        gh_pr.head.sha = "abc"
        github.get_repo.return_value.get_pull.return_value = gh_pr

        assert pulls.pr_has_conflicts(PRRef(number=9, url="u", head_ref="h"), "abc") is expected

    def test_pr_has_conflicts_unknown_when_auth_fails(
        self, pulls: GitHubPullRequestClient, auth: MagicMock
    ) -> None:
        auth.installation_for.side_effect = RuntimeError("no installation")

        assert pulls.pr_has_conflicts(PRRef(number=9, url="u", head_ref="h"), "abc") is None


class TestPRHeadMismatch:
    def test_still_raised_by_the_shared_lookup(
        self, auth: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Should the lookup's own backstop ever let a PR on another branch
        # through, the port refuses to report it.
        pulls = GitHubPullRequestClient(auth, REPO, label_agent_generated="x")
        stray = _gh_pr(5, "other")
        monkeypatch.setattr(
            "issue_agent.providers.github.client.GitHubClient.find_pr_by_branch",
            lambda self, full_name, branch: stray,
        )

        with pytest.raises(PRHeadMismatchError):
            pulls.find_open_pr("agent/issue-1")


class TestTransport:
    @pytest.fixture
    def transport(self, auth: MagicMock) -> GitHubTransport:
        return GitHubTransport(auth, "https://ghe.example.com")

    def test_urls(self, transport: GitHubTransport) -> None:
        assert transport.clone_url(REPO) == f"https://ghe.example.com/{REPO}.git"
        assert transport.pr_url(REPO, 3) == f"https://ghe.example.com/{REPO}/pull/3"
        assert (
            transport.compare_url(REPO, "main", "agent/issue-1")
            == f"https://ghe.example.com/{REPO}/compare/main...agent/issue-1"
        )

    def test_git_auth_env_uses_the_repos_token_on_the_host(
        self, transport: GitHubTransport, auth: MagicMock
    ) -> None:
        env = transport.git_auth_env(REPO)

        auth.token_for.assert_called_once_with(REPO)
        assert env["GIT_CONFIG_KEY_0"] == "http.https://ghe.example.com.extraHeader"
        credential = base64.b64encode(b"x-access-token:ghs_token").decode()
        assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {credential}"
        assert "ghs_token" not in transport.clone_url(REPO)

    def test_format_ref(self, transport: GitHubTransport) -> None:
        assert transport.format_ref("issue", 4, repo=REPO, from_repo=REPO) == "#4"
        assert transport.format_ref("pr", 4, repo=REPO, from_repo=REPO) == "#4"
        assert transport.format_ref("issue", 4, repo="org/issues", from_repo=REPO) == "org/issues#4"

    def test_closing_reference(self, transport: GitHubTransport) -> None:
        assert transport.closing_reference(REPO, 4, REPO) == "Closes #4"
        assert transport.closing_reference("org/issues", 4, REPO) == "Closes org/issues#4"


def _plugin(**overrides: Any) -> GitHubProviderPlugin:
    fields: dict[str, Any] = {
        "webhook_secret": "s",
        "identity": BotIdentity(app_id=1, app_name="Agent", app_bot_login="agent[bot]"),
        "private_key": PEM,
    }
    fields.update(overrides)
    return GitHubProviderPlugin("github", **fields)


class TestPlugin:
    def test_auth_is_built_once_on_first_outbound_use(self, auth: MagicMock) -> None:
        factory = MagicMock(return_value=auth)
        plugin = _plugin(auth_factory=factory)

        factory.assert_not_called()
        plugin.client("a/b")
        plugin.pulls("a/b")
        plugin.transport()
        factory.assert_called_once_with()

    def test_without_credentials_outbound_use_fails(self) -> None:
        with pytest.raises(RuntimeError, match="no GitHub App credentials"):
            _plugin().client("a/b")

    def test_clients_are_bound_to_the_repo(self, auth: MagicMock) -> None:
        plugin = _plugin(auth_factory=lambda: auth)

        plugin.client("org/issues").get_default_branch()

        auth.installation_for.assert_called_with("org/issues")

    def test_healthy_with_full_config(self) -> None:
        health = _plugin().health_check()

        assert health.ok is True
        assert health.checks == {"config": "ok", "auth": "ok", "own_bot_login": "ok"}

    @pytest.mark.parametrize(
        ("overrides", "check", "message"),
        [
            ({"webhook_secret": ""}, "config", "missing: PROVIDERS__GITHUB__WEBHOOK_SECRET"),
            (
                {"identity": BotIdentity(app_id=0, app_name="A", app_bot_login="b")},
                "config",
                "missing: providers.github.app_id",
            ),
            ({"private_key": ""}, "auth", "private key not loaded"),
            ({"private_key": "nope"}, "auth", "private key does not look like a valid PEM"),
        ],
    )
    def test_unhealthy(self, overrides: dict[str, Any], check: str, message: str) -> None:
        health = _plugin(**overrides).health_check()

        assert health.ok is False
        assert health.checks[check] == message

    def test_missing_bot_login_is_only_a_warning(self) -> None:
        health = _plugin(identity=BotIdentity(app_id=1, app_name="Agent")).health_check()

        assert health.ok is True
        assert health.checks["own_bot_login"].startswith("warning:")
