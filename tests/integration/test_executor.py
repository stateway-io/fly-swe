from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.providers.github.client import GitHubClient
from issue_agent.queue.models import Task, TaskStatus, TriggerKind
from issue_agent.queue.store import SQLiteStore
from issue_agent.runner.executor import (
    CHECKS_SENTINEL_FILENAME,
    TEST_COMMAND_SENTINEL_FILENAME,
    AgentResult,
    run_task,
)
from issue_agent.runner.executor import _run_git as _real_run_git
from tests.fakes.fake_provider import FakeProviderWithPulls, FakeTransport
from tests.helpers.factories import make_author, make_issue, make_trigger_event
from tests.helpers.github import stub_github_plugin
from tests.helpers.settings import github_providers

_TRIGGER = make_trigger_event(
    issue=make_issue(
        number=42,
        title="Fix broken link in docs",
        body="The README has a broken link.",
        labels=["docs"],
    ),
    actor=make_author(login="testuser"),
)


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


def _make_settings(**overrides: dict) -> Settings:
    defaults: dict = dict(
        PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
        LLM_API_KEY="k",
        DATA_DIR="/tmp/test-executor-int-data",
        REPO_ROUTES=[
            {
                "issues": "stateway-io/stateway-issues",
                "target": "stateway-io/stateway-agent-fixtures",
            }
        ],
    )
    defaults.update(overrides)
    return SettingsNoEnv(**defaults)


def _make_task(**overrides: dict) -> Task:
    defaults: dict = dict(
        delivery_id="delivery-int-1",
        repo="stateway-io/stateway-issues",
        issue_number=42,
        trigger_kind=TriggerKind.ISSUE_OPENED,
        trigger_json=_TRIGGER.model_dump_json(),
        target_repo="stateway-io/stateway-agent-fixtures",
        base_branch="main",
        workflow="trunk_based",
    )
    defaults.update(overrides)
    return Task(**defaults)


class FakeFileWriterRunner:
    """Writes a file to the workspace simulating what a real agent would do."""

    def run(self, workspace_path: str, task: Task, settings: Settings, route=None, resume_summary=None, on_progress=None, toolchain_env=None, branch_status=None, comment_history=None, terms=None) -> AgentResult:
        ws = Path(workspace_path)
        (ws / "src").mkdir(parents=True, exist_ok=True)
        (ws / "src" / "fix.md").write_text("fixed by agent\n")
        return AgentResult(success=True, cost_usd=0.42, iterations=7)


class FakeFileAndSentinelWriterRunner:
    """Like FakeFileWriterRunner, but also writes the ``.agent-test-command``
    and ``.agent-checks`` sentinels described in prompts/system.md —
    simulating an agent that discovered/ran its own test command, plus a
    check it could not run, and is reporting both back."""

    def run(self, workspace_path: str, task: Task, settings: Settings, route=None, resume_summary=None, on_progress=None, toolchain_env=None, branch_status=None, comment_history=None, terms=None) -> AgentResult:
        ws = Path(workspace_path)
        (ws / "src").mkdir(parents=True, exist_ok=True)
        (ws / "src" / "fix.md").write_text("fixed by agent\n")
        (ws / TEST_COMMAND_SENTINEL_FILENAME).write_text("true\n")
        (ws / CHECKS_SENTINEL_FILENAME).write_text(
            "passed: true\nnot_run: npm run test:integration — needs Postgres\n"
        )
        return AgentResult(success=True, cost_usd=0.42, iterations=7)


class SentinelOnlyRunner:
    """Writes ONLY the sentinel file — no real code change at all. Used to
    confirm the sentinel can't be mistaken for a real change and bypass the
    ``no_changes`` abort (Finding 1)."""

    def run(self, workspace_path: str, task: Task, settings: Settings, route=None, resume_summary=None, on_progress=None, toolchain_env=None, branch_status=None, comment_history=None, terms=None) -> AgentResult:
        (Path(workspace_path) / TEST_COMMAND_SENTINEL_FILENAME).write_text("true\n")
        return AgentResult(success=True, cost_usd=0.10, iterations=3)


@pytest.fixture(autouse=True)
def _cleanup_data_dir() -> None:
    yield
    import shutil

    shutil.rmtree("/tmp/test-executor-int-data", ignore_errors=True)


@pytest.fixture
def store() -> SQLiteStore:
    s = SQLiteStore(":memory:")
    s.init()
    return s


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return _make_settings(DATA_DIR=str(tmp_path / "data"))


@pytest.fixture
def git_workspace(tmp_path: Path) -> Path:
    repo = tmp_path / "local-stateway"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "bot@test.com"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test Bot"],
        cwd=repo, check=True, capture_output=True,
    )
    (repo / "README.md").write_text("# Stateway\n\nWelcome.")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=repo, check=True, capture_output=True,
    )

    bare = tmp_path / "remote.git"
    bare.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare"], cwd=bare, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=repo, check=True, capture_output=True,
    )
    return repo


@pytest.fixture
def pr_mock() -> MagicMock:
    pr = MagicMock()
    pr.number = 201
    pr.html_url = "https://github.com/stateway-io/stateway-agent-fixtures/pull/201"
    pr.head.ref = "agent/issue-42-fix"
    return pr


class TestExecutorIntegration:
    def test_full_flow_with_real_git(
        self,
        store: SQLiteStore,
        settings: Settings,
        git_workspace: Path,
        pr_mock: MagicMock,
    ) -> None:
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        with (
            patch("issue_agent.runner.executor.Workspace") as mock_ws_cls,
            patch("issue_agent.runner.executor._resolve_agent_runner") as mock_resolve,
            patch("issue_agent.runner.executor._run_git") as mock_git,
            patch.object(GitHubClient, "create_pr", return_value=pr_mock),
            patch.object(GitHubClient, "create_comment"),
            patch.object(GitHubClient, "find_pr_by_branch", return_value=None),
            patch.object(GitHubClient, "get_repo") as mock_get_repo,
        ):
            mock_get_repo.return_value.get_issue.return_value.add_to_labels = MagicMock()

            mock_ws = MagicMock()
            mock_ws.clone.return_value = git_workspace
            mock_ws_cls.return_value = mock_ws

            mock_resolve.return_value = FakeFileWriterRunner()

            def _selective_git(*args, **kwargs):
                if args and len(args) > 1 and args[1] in ("remote", "push"):
                    return ""
                return _real_run_git(*args, **kwargs)

            mock_git.side_effect = _selective_git

            run_task(store, fetched, settings, stub_github_plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.SUCCEEDED
        assert updated.pr_number == 201
        assert updated.cost_usd == 0.42
        assert updated.iterations == 7

        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=git_workspace,
        )
        branch = result.stdout.strip()
        assert branch.startswith("agent/issue-42-")

        result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            capture_output=True, text=True, cwd=git_workspace,
        )
        # task.repo ("stateway-io/stateway-issues") differs from
        # task.target_repo ("stateway-io/stateway-agent-fixtures") in this
        # fixture, so the commit message's issue ref must be qualified with
        # the issue's own repo (bug A4) rather than a bare "#42" that would
        # point at the wrong repo's issue tracker.
        assert "fix(stateway-io/stateway-issues#42)" in result.stdout

        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=git_workspace,
        )
        assert result.stdout.strip() == "", f"Expected clean workspace, got: {result.stdout}"

    def test_no_changes_with_real_git(
        self,
        store: SQLiteStore,
        settings: Settings,
        git_workspace: Path,
    ) -> None:
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        report = (
            "The requested change is already in the repository: the header "
            "was rewritten in 35c7c55 (PR #261). This issue can be closed "
            "with no change."
        )

        class NoOpRunner:
            def run(self, workspace_path: str, task: Task, settings: Settings, route=None, resume_summary=None, on_progress=None, toolchain_env=None, branch_status=None, comment_history=None, terms=None) -> AgentResult:
                return AgentResult(
                    success=True, cost_usd=0.10, iterations=3, submission=report
                )

        with (
            patch("issue_agent.runner.executor.Workspace") as mock_ws_cls,
            patch("issue_agent.runner.executor._resolve_agent_runner") as mock_resolve,
            patch.object(GitHubClient, "create_comment") as mock_create_comment,
        ):
            mock_ws = MagicMock()
            mock_ws.clone.return_value = git_workspace
            mock_ws_cls.return_value = mock_ws

            mock_resolve.return_value = NoOpRunner()

            run_task(store, fetched, settings, stub_github_plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.ABORTED_POLICY

        # initial "started work" comment (R1) + the outcome comment
        assert mock_create_comment.call_count == 2

        # The outcome comment is what a reviewer reads. It has to
        # carry the agent's provenance report and read as a verdict, not as a
        # failed run.
        outcome = mock_create_comment.call_args_list[-1].args[2]
        assert "35c7c55" in outcome
        assert "closed with no change" in outcome
        assert "aborted" not in outcome.lower()

    def test_sentinel_file_excluded_from_commit_and_pr(
        self,
        store: SQLiteStore,
        settings: Settings,
        git_workspace: Path,
        pr_mock: MagicMock,
    ) -> None:
        """Finding 1(a): the agent's ``.agent-test-command`` sentinel must
        never be committed, pushed, or listed in the PR — even though it's
        written into the git repo root during the agent's own run, before
        executor.py's git bookkeeping starts. Uses real git throughout
        (only `remote`/`push` are stubbed, same as
        test_full_flow_with_real_git, to avoid needing a real GitHub push)."""
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        with (
            patch("issue_agent.runner.executor.Workspace") as mock_ws_cls,
            patch("issue_agent.runner.executor._resolve_agent_runner") as mock_resolve,
            patch("issue_agent.runner.executor._run_git") as mock_git,
            patch.object(GitHubClient, "create_pr", return_value=pr_mock) as mock_create_pr,
            patch.object(GitHubClient, "create_comment"),
            patch.object(GitHubClient, "find_pr_by_branch", return_value=None),
            patch.object(GitHubClient, "get_repo") as mock_get_repo,
        ):
            mock_get_repo.return_value.get_issue.return_value.add_to_labels = MagicMock()

            mock_ws = MagicMock()
            mock_ws.clone.return_value = git_workspace
            mock_ws_cls.return_value = mock_ws

            mock_resolve.return_value = FakeFileAndSentinelWriterRunner()

            def _selective_git(*args, **kwargs):
                if args and len(args) > 1 and args[1] in ("remote", "push"):
                    return ""
                return _real_run_git(*args, **kwargs)

            mock_git.side_effect = _selective_git

            run_task(store, fetched, settings, stub_github_plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.SUCCEEDED

        # The sentinels must not exist on disk after the run...
        assert not (git_workspace / TEST_COMMAND_SENTINEL_FILENAME).exists()
        assert not (git_workspace / CHECKS_SENTINEL_FILENAME).exists()

        # ...must not appear in the commit that was made...
        result = subprocess.run(
            ["git", "show", "--stat", "--oneline", "HEAD"],
            capture_output=True, text=True, cwd=git_workspace,
        )
        assert TEST_COMMAND_SENTINEL_FILENAME not in result.stdout
        assert CHECKS_SENTINEL_FILENAME not in result.stdout

        # ...the workspace must be fully clean (no stray untracked file)...
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=git_workspace,
        )
        assert result.stdout.strip() == "", f"Expected clean workspace, got: {result.stdout}"

        # ...and it must not appear in the PR body's "Files changed" list.
        pr_body = mock_create_pr.call_args.kwargs["body"]
        assert TEST_COMMAND_SENTINEL_FILENAME not in pr_body
        assert CHECKS_SENTINEL_FILENAME not in pr_body

        # The harness re-ran `true`, and the agent's partial report follows
        # under its own label (issue #102).
        assert "- [x] `true` passed" in pr_body
        assert "Reported by the agent (not re-run by the harness):" in pr_body
        assert "- `npm run test:integration` — not run: needs Postgres" in pr_body

    def test_sentinel_only_change_still_triggers_no_changes_abort(
        self,
        store: SQLiteStore,
        settings: Settings,
        git_workspace: Path,
    ) -> None:
        """Finding 1(b): if the ONLY thing the agent wrote is the sentinel
        file, the task must still be treated as a no-op — the sentinel must
        not make an otherwise-clean workspace look "dirty" enough to bypass
        the `no_changes` abort. Real git throughout, nothing mocked."""
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        with (
            patch("issue_agent.runner.executor.Workspace") as mock_ws_cls,
            patch("issue_agent.runner.executor._resolve_agent_runner") as mock_resolve,
            patch.object(GitHubClient, "create_comment") as mock_create_comment,
        ):
            mock_ws = MagicMock()
            mock_ws.clone.return_value = git_workspace
            mock_ws_cls.return_value = mock_ws

            mock_resolve.return_value = SentinelOnlyRunner()

            run_task(store, fetched, settings, stub_github_plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.ABORTED_POLICY
        assert updated.error == "no_changes"

        # initial "started work" comment (R1) + the outcome comment
        assert mock_create_comment.call_count == 2
        assert not (git_workspace / TEST_COMMAND_SENTINEL_FILENAME).exists()

    def test_agent_failure_with_abort_comment(
        self,
        store: SQLiteStore,
        settings: Settings,
        git_workspace: Path,
    ) -> None:
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        class FailingRunner:
            def run(self, workspace_path: str, task: Task, settings: Settings, route=None, resume_summary=None, on_progress=None, toolchain_env=None, branch_status=None, comment_history=None, terms=None) -> AgentResult:
                return AgentResult(success=False, error="agent_crashed", cost_usd=0.05, iterations=2)

        with (
            patch("issue_agent.runner.executor.Workspace") as mock_ws_cls,
            patch("issue_agent.runner.executor._resolve_agent_runner") as mock_resolve,
            patch.object(GitHubClient, "create_comment") as mock_create_comment,
        ):
            mock_ws = MagicMock()
            mock_ws.clone.return_value = git_workspace
            mock_ws_cls.return_value = mock_ws

            mock_resolve.return_value = FailingRunner()

            run_task(store, fetched, settings, stub_github_plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.FAILED_TERMINAL

        comment_body = mock_create_comment.call_args[0][2]
        assert "aborted" in comment_body.lower()
        assert "crashed unexpectedly" in comment_body

    def test_llm_failure_after_edits_commits_and_pushes_wip(
        self,
        store: SQLiteStore,
        settings: Settings,
        git_workspace: Path,
    ) -> None:
        """The provider connection drops mid-run after
        the agent already edited files. The edits must land on the work
        branch (committed + pushed) and the work must stay resumable."""
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        class DroppedConnectionRunner:
            def run(self, workspace_path: str, task: Task, settings: Settings, route=None, resume_summary=None, on_progress=None, toolchain_env=None, branch_status=None, comment_history=None, terms=None) -> AgentResult:
                (Path(workspace_path) / "src").mkdir(parents=True, exist_ok=True)
                (Path(workspace_path) / "src" / "fix.md").write_text("half-done fix\n")
                return AgentResult(
                    success=False, error="llm_error", cost_usd=0.06, iterations=49,
                    error_detail="APIError: [SSL: UNEXPECTED_EOF_WHILE_READING]",
                )

        pushes: list[tuple] = []

        def _selective_git(*args, **kwargs):
            if args and len(args) > 1 and args[1] == "push":
                pushes.append(args)
                return ""
            if args and len(args) > 1 and args[1] == "remote":
                return ""
            return _real_run_git(*args, **kwargs)

        with (
            patch("issue_agent.runner.executor.Workspace") as mock_ws_cls,
            patch("issue_agent.runner.executor._resolve_agent_runner") as mock_resolve,
            patch("issue_agent.runner.executor._run_git") as mock_git,
            patch.object(GitHubClient, "create_comment") as mock_create_comment,
        ):
            mock_ws = MagicMock()
            mock_ws.clone.return_value = git_workspace
            mock_ws_cls.return_value = mock_ws
            mock_resolve.return_value = DroppedConnectionRunner()
            mock_git.side_effect = _selective_git

            run_task(store, fetched, settings, stub_github_plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.FAILED_RECOVERABLE
        assert updated.error == "llm_error"

        # The edit is committed on the work branch, not just left in the tree.
        head_msg = subprocess.run(
            ["git", "log", "-1", "--format=%s"], cwd=git_workspace,
            check=True, capture_output=True, text=True,
        ).stdout
        assert "WIP" in head_msg and "llm_error" in head_msg
        tracked = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=git_workspace,
            check=True, capture_output=True, text=True,
        ).stdout
        assert "src/fix.md" in tracked
        assert pushes, "the WIP branch was never pushed"

        work = store.get_issue_work("github", task.repo, task.issue_number)
        assert work is not None
        assert work.state == "open"
        assert work.last_error == "llm_error"
        assert "preserved" in mock_create_comment.call_args[0][2]


class TestResumedBranchAlreadyAhead:
    """issue #89: a resumed run that writes nothing must still open a PR for
    the commits the work branch already carries, instead of discarding them
    as `no_changes`."""

    BRANCH = "agent/issue-42-fix-broken-link"

    def _push_work_branch(self, repo: Path) -> None:
        """Simulate the previous run: a commit on the work branch, pushed,
        with the local checkout left back on main as a fresh clone would be."""
        for args in (
            ["git", "checkout", "-b", self.BRANCH],
            ["git", "add", "-A"],
            ["git", "commit", "-m", "fix(#42): repair the broken link"],
            ["git", "push", "origin", self.BRANCH],
            ["git", "checkout", "main"],
            # A fresh clone has no local work branch — only the remote one.
            ["git", "branch", "-D", self.BRANCH],
        ):
            if args[1] == "add":
                (repo / "LINKS.md").write_text("Fixed link.\n")
            subprocess.run(args, cwd=repo, check=True, capture_output=True)

    def _run(
        self,
        store: SQLiteStore,
        settings: Settings,
        git_workspace: Path,
        pr_mock: MagicMock,
        report: str,
    ):
        task = _make_task()
        store.insert_task(task)
        store.open_issue_work(
            "github",
            task.repo, task.issue_number, task.target_repo, task.base_branch, self.BRANCH
        )
        fetched = store.fetch_next()

        class NoOpRunner:
            def run(self, workspace_path: str, task: Task, settings: Settings, route=None, resume_summary=None, on_progress=None, toolchain_env=None, branch_status=None, comment_history=None, terms=None) -> AgentResult:
                return AgentResult(
                    success=True, cost_usd=0.10, iterations=3, submission=report
                )

        with (
            patch("issue_agent.runner.executor.Workspace") as mock_ws_cls,
            patch("issue_agent.runner.executor._resolve_agent_runner") as mock_resolve,
            patch("issue_agent.runner.executor._run_git") as mock_git,
            patch.object(GitHubClient, "create_pr", return_value=pr_mock) as mock_create_pr,
            patch.object(GitHubClient, "create_comment") as mock_create_comment,
            patch.object(GitHubClient, "find_pr_by_branch", return_value=None),
            patch.object(GitHubClient, "get_repo") as mock_get_repo,
        ):
            mock_get_repo.return_value.get_issue.return_value.add_to_labels = MagicMock()

            mock_ws = MagicMock()
            mock_ws.clone.return_value = git_workspace
            mock_ws_cls.return_value = mock_ws

            mock_resolve.return_value = NoOpRunner()

            def _selective_git(*args, **kwargs):
                if args and len(args) > 1 and args[1] == "push":
                    return ""
                return _real_run_git(*args, **kwargs)

            mock_git.side_effect = _selective_git

            run_task(store, fetched, settings, stub_github_plugin(settings))

        return task, mock_create_pr, mock_create_comment

    def test_opens_a_pr_for_the_commit_the_branch_already_carries(
        self,
        store: SQLiteStore,
        settings: Settings,
        git_workspace: Path,
        pr_mock: MagicMock,
    ) -> None:
        self._push_work_branch(git_workspace)
        report = (
            "The requested change is already in the repository: the link was "
            "repaired in a1b2c3d. This issue can be closed with no change."
        )

        task, mock_create_pr, mock_create_comment = self._run(
            store, settings, git_workspace, pr_mock, report
        )

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.SUCCEEDED
        assert updated.pr_number == 201

        work = store.get_issue_work("github", task.repo, task.issue_number)
        assert work is not None
        assert work.closed_reason == "pr_opened"

        # The PR is for THIS branch, and its body lists what the branch holds
        # plus a harness note — the agent's own report says "close with no
        # change", which on its own would read as "nothing to do here".
        kwargs = mock_create_pr.call_args.kwargs
        assert kwargs["head"] == self.BRANCH
        assert "LINKS.md" in kwargs["body"]
        assert "Opened by the harness" in kwargs["body"]
        assert "1 commit ahead of `main`" in kwargs["body"]

        outcome = mock_create_comment.call_args_list[-1].args[2]
        assert "already committed" in outcome
        assert self.BRANCH in outcome
        assert "aborted" not in outcome.lower()

    def test_still_aborts_when_the_branch_holds_nothing(
        self,
        store: SQLiteStore,
        settings: Settings,
        git_workspace: Path,
        pr_mock: MagicMock,
    ) -> None:
        """The work branch exists but is level with main: the verdict really
        is `no_changes` and must stay one."""
        subprocess.run(
            ["git", "push", "origin", f"main:{self.BRANCH}"],
            cwd=git_workspace, check=True, capture_output=True,
        )

        task, mock_create_pr, mock_create_comment = self._run(
            store, settings, git_workspace, pr_mock, "Nothing to do here."
        )

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.ABORTED_POLICY
        mock_create_pr.assert_not_called()


class TestExecutorOnAnotherForge:
    """A whole FIX run on the fake forge — GitLab-like references, a title
    prefix for drafts, no auto-close keyword and a 1,000-char body limit —
    so nothing GitHub-shaped can be left in the executor's path."""

    class _VerboseRunner:
        def run(self, workspace_path: str, task: Task, settings: Settings, route=None, resume_summary=None, on_progress=None, toolchain_env=None, branch_status=None, comment_history=None, terms=None) -> AgentResult:
            (Path(workspace_path) / "fix.md").write_text("fixed\n")
            return AgentResult(
                success=True, cost_usd=0.1, iterations=2, submission="Long report. " * 200
            )

    def test_full_flow_on_the_fake_forge(
        self, store: SQLiteStore, tmp_path: Path, git_workspace: Path
    ) -> None:
        settings = _make_settings(
            DATA_DIR=str(tmp_path / "data"),
            REPO_ROUTES=[
                {
                    "issues": "stateway-io/stateway-issues",
                    "target": "stateway-io/stateway-agent-fixtures",
                    "draft": True,
                }
            ],
        )
        plugin = FakeProviderWithPulls("fake", secret="s")
        forge = plugin.forge
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        with (
            patch("issue_agent.runner.executor.Workspace") as mock_ws_cls,
            patch(
                "issue_agent.runner.executor._resolve_agent_runner",
                return_value=self._VerboseRunner(),
            ),
        ):
            mock_ws_cls.return_value.clone.return_value = git_workspace
            run_task(store, fetched, settings, plugin)

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.SUCCEEDED
        assert updated.pr_number == 100

        # The clone goes through the forge's own transport.
        assert isinstance(mock_ws_cls.call_args[0][3], FakeTransport)
        details = forge.pr_details[("stateway-io/stateway-agent-fixtures", 100)]
        assert details["base"] == "main"
        # Draft by title prefix; the issue named with the forge's syntax and
        # never promised to close; the body cut to the forge's limit.
        assert details["draft"] is False
        assert str(details["title"]).startswith("WIP: [agent] fix(stateway-io/stateway-issues#42)")
        body = str(details["body"])
        assert "Closes" not in body
        assert len(body) == plugin.capabilities.max_body_length
        assert body.endswith("(truncated)")
        assert forge.labels[("stateway-io/stateway-agent-fixtures", 100)] == []

        log = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            capture_output=True, text=True, cwd=git_workspace, check=True,
        ).stdout
        assert log.startswith("fix(stateway-io/stateway-issues#42)")
        # The push authenticated with a credential the transport minted.
        assert forge.tokens

        comments = forge.posted("stateway-io/stateway-issues", 42)
        assert len(comments) == 2  # initial + fix
        assert "https://forge.example/stateway-io/stateway-agent-fixtures/-/merge_requests/100" in comments[-1]
