from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from github import Github
from github.Issue import Issue
from github.IssueComment import IssueComment
from github.PullRequest import PullRequest
from github.Repository import Repository
from httpx import ASGITransport, AsyncClient
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.core.models import PRRef
from issue_agent.providers.github.auth import GitHubAppAuth
from issue_agent.providers.github.client import GitHubClient
from issue_agent.providers.github.plugin import GitHubProviderPlugin
from issue_agent.providers.registry import ProviderRegistry
from issue_agent.queue.models import TaskStatus
from issue_agent.worker import run_task
from tests.helpers.settings import github_providers

from .conftest import make_github_payload, sign_payload

TEST_INSTALLATION_ID = 98765

# ── Settings helper (same pattern as conftest and other test files) ──


class _SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


def _make_settings(**overrides: dict) -> Settings:
    defaults = dict(
        PROVIDERS=github_providers(
            app_id="123",
            app_private_key="-----BEGIN RSA PRIVATE KEY-----\nMOCKKEY\n-----END RSA PRIVATE KEY-----",
            webhook_secret="test-webhook-secret",
        ),
        LLM_API_KEY="sk-test-anthropic",
        REPO_ROUTES=[
            {
                "issues": "stateway-io/stateway-issues",
                "target": "stateway-io/stateway-agent-fixtures",
            }
        ],
    )
    defaults.update(overrides)
    return _SettingsNoEnv(**defaults)


# ── Helpers ────────────────────────────────────────────────────────


def _make_mock_auth() -> MagicMock:
    auth = MagicMock()
    auth.token = "ghs_test_token_integration"
    auth.expires_at = datetime.now(UTC) + timedelta(hours=1)
    return auth


def _setup_pygithub_mocks(
    mock_github_instance: MagicMock,
    repo_full_name: str = "stateway-io/stateway",
) -> tuple[MagicMock, MagicMock, MagicMock]:
    mock_repo = MagicMock(spec=Repository)
    mock_issue = MagicMock(spec=Issue)
    mock_pr = MagicMock(spec=PullRequest)
    mock_comment = MagicMock(spec=IssueComment)

    mock_github_instance.get_repo.return_value = mock_repo
    mock_repo.get_issue.return_value = mock_issue
    mock_issue.create_comment.return_value = mock_comment
    mock_repo.create_pull.return_value = mock_pr
    mock_pr.number = 42

    return mock_repo, mock_issue, mock_pr


# ── TestAuthClientWiring ───────────────────────────────────────────


class TestAuthClientWiring:
    """Verifies the real wiring between GitHubAppAuth and GitHubClient,
    mocking only PyGithub (GithubIntegration + Github)."""

    @pytest.fixture
    def settings(self) -> Settings:
        return _make_settings()

    @pytest.fixture
    def auth(self, settings: Settings) -> GitHubAppAuth:
        return GitHubAppAuth(settings.providers["github"])

    @pytest.fixture
    def client(self, auth: GitHubAppAuth) -> GitHubClient:
        return GitHubClient(auth, TEST_INSTALLATION_ID)

    def test_get_repo_propagates_to_github(self, settings: Settings) -> None:
        with (
            patch("issue_agent.providers.github.auth.GithubIntegration") as mi,
            patch("issue_agent.providers.github.auth.Github") as mg,
        ):
            mock_auth = _make_mock_auth()
            mi.return_value.get_access_token.return_value = mock_auth

            mock_gh = MagicMock(spec=Github)
            mg.return_value = mock_gh

            mock_repo = MagicMock(spec=Repository)
            mock_gh.get_repo.return_value = mock_repo

            auth = GitHubAppAuth(settings.providers["github"])
            client = GitHubClient(auth, TEST_INSTALLATION_ID)

            result = client.get_repo("owner/test-repo")

        assert result is mock_repo
        mock_gh.get_repo.assert_called_once_with("owner/test-repo")

    def test_token_cached_across_calls(self, settings: Settings) -> None:
        with (
            patch("issue_agent.providers.github.auth.GithubIntegration") as mi,
            patch("issue_agent.providers.github.auth.Github") as mg,
        ):
            mock_auth = _make_mock_auth()
            mi.return_value.get_access_token.return_value = mock_auth

            mock_gh = MagicMock(spec=Github)
            mg.return_value = mock_gh

            mock_repo = MagicMock(spec=Repository)
            mock_gh.get_repo.return_value = mock_repo

            auth = GitHubAppAuth(settings.providers["github"])
            client = GitHubClient(auth, TEST_INSTALLATION_ID)

            client.get_repo("a/b")
            client.get_repo("c/d")

        # GithubIntegration.get_access_token called once (cached)
        mi.return_value.get_access_token.assert_called_once_with(98765)

    def test_get_issue_through_full_stack(self, settings: Settings) -> None:
        with (
            patch("issue_agent.providers.github.auth.GithubIntegration") as mi,
            patch("issue_agent.providers.github.auth.Github") as mg,
        ):
            mock_auth = _make_mock_auth()
            mi.return_value.get_access_token.return_value = mock_auth

            mock_gh = MagicMock(spec=Github)
            mg.return_value = mock_gh

            mock_repo = MagicMock(spec=Repository)
            mock_issue = MagicMock(spec=Issue)
            mock_gh.get_repo.return_value = mock_repo
            mock_repo.get_issue.return_value = mock_issue

            auth = GitHubAppAuth(settings.providers["github"])
            client = GitHubClient(auth, TEST_INSTALLATION_ID)

            result = client.get_issue("stateway-io/stateway", 7)

        assert result is mock_issue
        mock_repo.get_issue.assert_called_once_with(7)


# ── TestPullRequestsFullStack ──────────────────────────────────────


class TestPullRequestsFullStack:
    """The GitHub plugin's PR and issue clients over the real GitHubAppAuth
    and GitHubClient, mocking only PyGithub."""

    REPO = "stateway-io/stateway"

    @pytest.fixture
    def settings(self) -> Settings:
        return _make_settings()

    @pytest.fixture
    def github(self):
        with (
            patch("issue_agent.providers.github.auth.GithubIntegration") as mi,
            patch("issue_agent.providers.github.auth.Github") as mg,
        ):
            mi.return_value.get_access_token.return_value = _make_mock_auth()
            mi.return_value.get_repo_installation.return_value.id = TEST_INSTALLATION_ID
            mock_gh = MagicMock(spec=Github)
            mg.return_value = mock_gh
            mock_repo, mock_issue, mock_pr = _setup_pygithub_mocks(mock_gh)
            mock_pr.html_url = f"https://github.com/{self.REPO}/pull/42"
            mock_pr.head.ref = "agent/issue-42-fix-typo"
            yield mock_repo, mock_issue, mock_pr

    def test_create_pr_adds_label(self, settings: Settings, github) -> None:
        mock_repo, mock_issue, _ = github
        pulls = GitHubProviderPlugin.from_settings(
            "github", settings.providers["github"], settings
        ).pulls(self.REPO)

        pr = pulls.create_pr(
            head="agent/issue-42-fix-typo",
            base="main",
            title="[agent] fix(#42): Fix typo",
            body="## Summary\nFixed a typo.",
            draft=False,
            labels=[],
        )

        assert pr.number == 42
        assert pr.url == f"https://github.com/{self.REPO}/pull/42"
        mock_repo.create_pull.assert_called_once()
        mock_repo.get_issue.assert_called_once_with(42)
        mock_issue.add_to_labels.assert_called_once_with("agent-generated")

    def test_find_open_pr_returns_the_existing_one(self, settings: Settings, github) -> None:
        mock_repo, _, _ = github
        existing = MagicMock(spec=PullRequest)
        existing.number = 7
        existing.html_url = f"https://github.com/{self.REPO}/pull/7"
        existing.head.ref = "agent/issue-42-fix-typo"
        mock_repo.get_pulls.return_value = [existing]
        pulls = GitHubProviderPlugin.from_settings(
            "github", settings.providers["github"], settings
        ).pulls(self.REPO)

        pr = pulls.find_open_pr("agent/issue-42-fix-typo")

        assert pr is not None and pr.number == 7
        mock_repo.create_pull.assert_not_called()

    def test_create_pr_passes_arguments_correctly(self, settings: Settings, github) -> None:
        mock_repo, _, _ = github
        pulls = GitHubProviderPlugin.from_settings(
            "github", settings.providers["github"], settings
        ).pulls(self.REPO)

        title = "[agent] fix(#7): Update deps"
        body = "## Summary\nUpdated dependencies."
        pulls.create_pr(
            head="agent/issue-7-update-deps",
            base="develop",
            title=title,
            body=body,
            draft=False,
            labels=[],
        )

        mock_repo.create_pull.assert_called_once_with(
            title=title,
            body=body,
            head="agent/issue-7-update-deps",
            base="develop",
            draft=False,
        )

    def test_comment_on_issue(self, settings: Settings, github) -> None:
        _, mock_issue, _ = github
        forge = GitHubProviderPlugin.from_settings(
            "github", settings.providers["github"], settings
        ).client(self.REPO)

        url = forge.post_comment(42, "Hello from agent")

        assert url is mock_issue.create_comment.return_value.html_url
        mock_issue.create_comment.assert_called_once_with("Hello from agent")

    def test_comment_on_pr(self, settings: Settings, github) -> None:
        _, mock_issue, _ = github
        pulls = GitHubProviderPlugin.from_settings(
            "github", settings.providers["github"], settings
        ).pulls(self.REPO)

        pulls.comment_on_pr(PRRef(number=99, url="u", head_ref="b"), "PR review comment")

        mock_issue.create_comment.assert_called_once_with("PR review comment")


# ── TestWorkerTriageWithGithubStack ────────────────────────────────


class TestWorkerTriageWithGithubStack:
    """Worker processing TRIAGE_ONLY tasks with real GitHub services,
    mocking only PyGithub (not mocked at the worker level)."""

    @pytest.mark.asyncio
    async def test_triage_comment_posts_to_github(self, store, app, worker_settings) -> None:
        body = make_github_payload(
            issue_number=20,
            labels=["question"],
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-gh-stack-1",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 202

        task = store.fetch_next()
        assert task is not None

        with (
            patch("issue_agent.providers.github.auth.GithubIntegration") as mi,
            patch("issue_agent.providers.github.auth.Github") as mg,
        ):
            mock_auth_obj = _make_mock_auth()
            mi.return_value.get_access_token.return_value = mock_auth_obj

            mock_gh = MagicMock(spec=Github)
            mg.return_value = mock_gh

            mock_repo = MagicMock(spec=Repository)
            mock_issue = MagicMock(spec=Issue)
            mock_comment = MagicMock(spec=IssueComment)
            mock_gh.get_repo.return_value = mock_repo
            mock_repo.get_issue.return_value = mock_issue
            mock_issue.create_comment.return_value = mock_comment

            run_task(store, task, worker_settings, ProviderRegistry.from_settings(worker_settings))

        final = store.get_task(task.id)
        assert final is not None
        assert final.status is TaskStatus.SUCCEEDED

        mock_issue.create_comment.assert_called_once()
        comment_body = mock_issue.create_comment.call_args[0][0]
        assert "Triage" in comment_body
        assert "@test-user" in comment_body

    @pytest.mark.asyncio
    async def test_triage_comment_uses_correct_category(self, store, app, worker_settings) -> None:
        body = make_github_payload(
            issue_number=21,
            labels=["question"],
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-gh-stack-2",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 202

        task = store.fetch_next()
        assert task is not None

        with (
            patch("issue_agent.providers.github.auth.GithubIntegration") as mi,
            patch("issue_agent.providers.github.auth.Github") as mg,
        ):
            mock_auth_obj = _make_mock_auth()
            mi.return_value.get_access_token.return_value = mock_auth_obj

            mock_gh = MagicMock(spec=Github)
            mg.return_value = mock_gh

            mock_repo = MagicMock(spec=Repository)
            mock_issue = MagicMock(spec=Issue)
            mock_comment = MagicMock(spec=IssueComment)
            mock_gh.get_repo.return_value = mock_repo
            mock_repo.get_issue.return_value = mock_issue
            mock_issue.create_comment.return_value = mock_comment

            run_task(store, task, worker_settings, ProviderRegistry.from_settings(worker_settings))

        comment_body = mock_issue.create_comment.call_args[0][0]
        assert "question" in comment_body.lower()

    @pytest.mark.asyncio
    async def test_github_exception_does_not_crash_task(self, store, app, worker_settings) -> None:
        body = make_github_payload(
            issue_number=22,
            labels=["question"],
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-gh-stack-3",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 202

        task = store.fetch_next()
        assert task is not None

        with (
            patch("issue_agent.providers.github.auth.GithubIntegration") as mi,
            patch("issue_agent.providers.github.auth.Github") as mg,
        ):
            mock_auth_obj = _make_mock_auth()
            mi.return_value.get_access_token.return_value = mock_auth_obj

            mock_gh = MagicMock(spec=Github)
            mg.return_value = mock_gh

            mock_repo = MagicMock(spec=Repository)
            mock_gh.get_repo.return_value = mock_repo
            mock_repo.get_issue.side_effect = Exception("GitHub API error")

            run_task(store, task, worker_settings, ProviderRegistry.from_settings(worker_settings))

        final = store.get_task(task.id)
        assert final is not None
        assert final.status is TaskStatus.SUCCEEDED

    @pytest.mark.asyncio
    async def test_triage_with_docs_label_posts_comment(self, store, app, worker_settings) -> None:
        body = make_github_payload(
            issue_number=23,
            labels=["question"],
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-gh-stack-4",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 202
