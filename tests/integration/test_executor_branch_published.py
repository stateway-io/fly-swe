"""The executor on a forge with no PR API (PRD §3.7).

The run pushes the work branch and comments on the issue with a compare link
instead of opening a PR; the work item closes as ``branch_published``, and a
maintainer's follow-up comment reopens it on the same branch, exactly like a
``pr_opened`` follow-up. Conflicts with the base are checked in the
workspace, since there is no PR to ask about.

Git runs for real (a bare remote plus fresh clones); the forge is the
in-memory ``FakeProvider``, so no GitHub code is on the path at all.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.core.models import Author
from issue_agent.queue.models import IssueWork, Task, TaskStatus, TriggerKind
from issue_agent.queue.store import SQLiteStore
from issue_agent.runner.executor import AgentResult, run_task
from tests.fakes.fake_provider import FAKE_HOST, FakeProvider
from tests.helpers.factories import make_author, make_issue, make_trigger_event
from tests.helpers.settings import github_providers

if TYPE_CHECKING:
    from collections.abc import Callable

ISSUES_REPO = "stateway-io/stateway-issues"
TARGET_REPO = "stateway-io/stateway-agent-fixtures"
BRANCH = "agent/issue-42-fix-broken-link-in-docs"
EARLIER_SUMMARY = "Earlier summary of the fix."

BASE_DOCS = "alpha\nbeta\ngamma\n"
BRANCH_DOCS = "alpha\nbeta-branch\ngamma\n"
BASE_ADVANCED_DOCS = "alpha\nbeta-main\ngamma\n"  # conflicts with BRANCH_DOCS


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _configure(repo: Path) -> None:
    _git(repo, "config", "user.email", "bot@test.com")
    _git(repo, "config", "user.name", "Test Bot")


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


class _Runner:
    """Writes ``edit`` into the workspace, after calling ``during`` (a stand-in
    for something happening on the forge while the agent works)."""

    def __init__(
        self, edit: dict[str, str] | None = None, during: Callable[[], None] | None = None
    ) -> None:
        self.edit = edit or {}
        self.during = during
        self.branch_status: str | None = None
        self.resume_summary: str | None = None

    def run(
        self, workspace_path: str, task: Task, settings: Settings, route=None,
        resume_summary=None, on_progress=None, toolchain_env=None, branch_status=None,
        comment_history=None, terms=None,
    ) -> AgentResult:
        self.branch_status = branch_status
        self.terms = terms
        self.resume_summary = resume_summary
        if self.during:
            self.during()
        _write(Path(workspace_path), self.edit)
        return AgentResult(
            success=True, cost_usd=0.2, iterations=3, submission="Repaired the link."
        )


class BranchEnv:
    def __init__(self, tmp_path: Path, store: SQLiteStore, settings: Settings) -> None:
        self.tmp = tmp_path
        self.store = store
        self.settings = settings
        self.plugin = FakeProvider("fake", secret="s")
        self.remote = tmp_path / "remote.git"
        self.remote.mkdir()
        _git(self.remote, "init", "--bare", "-b", "main")
        seed = self._clone_empty()
        _write(seed, {"docs.md": BASE_DOCS})
        _git(seed, "add", "-A")
        _git(seed, "commit", "-m", "initial commit")
        _git(seed, "push", "origin", "main")

    def _clone_empty(self) -> Path:
        seed = self.tmp / "seed"
        seed.mkdir()
        _git(seed, "init", "-b", "main")
        _configure(seed)
        _git(seed, "remote", "add", "origin", str(self.remote))
        return seed

    def _clone(self, name: str) -> Path:
        dest = self.tmp / f"{name}-{uuid.uuid4().hex[:6]}"
        _git(self.tmp, "clone", str(self.remote), str(dest))
        _configure(dest)
        return dest

    def publish(self, branch: str, files: dict[str, str], start: str = "main") -> str:
        clone = self._clone("pub")
        _git(clone, "checkout", "-B", branch, f"origin/{start}")
        _write(clone, files)
        _git(clone, "add", "-A")
        _git(clone, "commit", "-m", f"update {branch}")
        _git(clone, "push", "-f", "origin", branch)
        return _git(clone, "rev-parse", "HEAD")

    def seed_published_branch(self) -> str:
        """An earlier run's delivery: the branch on the remote and the work
        item closed as `branch_published`, with no PR number."""
        self.store.open_issue_work("github", ISSUES_REPO, 42, TARGET_REPO, "main", BRANCH)
        self.store.update_issue_work(
            "github",
            ISSUES_REPO, 42, state="closed", closed_reason="branch_published",
            last_summary=EARLIER_SUMMARY, last_summary_kind="submission",
        )
        return self.publish(BRANCH, {"docs.md": BRANCH_DOCS, "src/fix.md": "fixed\n"})

    def remote_has_branch(self, branch: str) -> bool:
        return bool(_git(self.remote, "branch", "--list", branch))

    def remote_contains(self, ancestor: str, ref: str) -> bool:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, ref],
            cwd=self.remote, capture_output=True,
        )
        return result.returncode == 0

    def work(self) -> IssueWork:
        work = self.store.get_issue_work("github", ISSUES_REPO, 42)
        assert work is not None
        return work

    def comments(self) -> list[str]:
        return self.plugin.forge.posted(ISSUES_REPO, 42)

    def run(self, runner: _Runner, kind: TriggerKind = TriggerKind.LABEL_ADDED) -> Task:
        ws_path = self._clone("ws")
        task = Task(
            delivery_id=f"delivery-{uuid.uuid4().hex[:6]}",
            repo=ISSUES_REPO,
            issue_number=42,
            trigger_kind=kind,
            trigger_json=make_trigger_event(
                kind,
                issue=make_issue(
                    repo=ISSUES_REPO, number=42, title="Fix broken link in docs",
                    author=Author(),
                ),
                actor=make_author(login="maintainer"),
                trigger_comment=None,
            ).model_dump_json(),
            target_repo=TARGET_REPO,
            base_branch="main",
            workflow="trunk_based",
        )
        self.store.insert_task(task)
        fetched = self.store.fetch_next()
        with (
            patch("issue_agent.runner.executor.Workspace") as mock_ws_cls,
            patch("issue_agent.runner.executor._resolve_agent_runner", return_value=runner),
        ):
            mock_ws_cls.return_value.clone.return_value = ws_path
            run_task(self.store, fetched, self.settings, self.plugin)
        updated = self.store.get_task(task.id)
        assert updated is not None
        return updated


@pytest.fixture
def env(tmp_path: Path) -> BranchEnv:
    store = SQLiteStore(":memory:")
    store.init()
    settings = SettingsNoEnv(
        PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
        LLM_API_KEY="k",
        DATA_DIR=str(tmp_path / "data"),
        REPO_ROUTES=[
            {
                "issues": ISSUES_REPO,
                "target": TARGET_REPO,
                "draft": True,
                "labels": ["needs-review"],
            }
        ],
    )
    return BranchEnv(tmp_path, store, settings)


COMPARE_URL = f"{FAKE_HOST}/{TARGET_REPO}/-/compare/main...{BRANCH}"


class TestFirstDelivery:
    def test_pushes_the_branch_and_comments_with_the_compare_link(
        self, env: BranchEnv
    ) -> None:
        task = env.run(_Runner({"docs.md": BRANCH_DOCS}))

        assert task.status is TaskStatus.SUCCEEDED
        assert task.pr_number is None
        work = env.work()
        assert work.state == "closed"
        assert work.closed_reason == "branch_published"
        assert work.pr_number is None
        assert work.last_summary == "Repaired the link."

        # The work is on the remote, and no PR exists anywhere.
        assert env.remote_has_branch(BRANCH)
        assert env.plugin.forge.prs == {}

        initial, delivered = env.comments()
        assert "started working on this issue" in initial
        assert f"pushed a proposed fix to branch `{BRANCH}`" in delivered
        assert f"[`main...{BRANCH}`]({COMPARE_URL})" in delivered
        assert "No Merge Request was opened" in delivered
        assert "## Summary\nRepaired the link." in delivered
        assert "`docs.md`" in delivered
        # Nothing ties the branch to the issue, so nothing promises to close it.
        assert "Closes" not in delivered

        # The labels a PR would carry go on the issue.
        assert env.plugin.forge.labels[(ISSUES_REPO, 42)] == ["agent-generated", "needs-review"]

    def test_the_issue_reference_uses_the_forge_syntax(self, env: BranchEnv) -> None:
        env.run(_Runner({"src/fix.md": "fixed\n"}))

        subject = _git(env.remote, "log", "-1", "--format=%s", BRANCH)
        assert subject.startswith(f"fix({ISSUES_REPO}#42)")

    def test_the_agent_is_prompted_in_the_forges_vocabulary(self, env: BranchEnv) -> None:
        runner = _Runner({"src/fix.md": "fixed\n"})
        env.run(runner)

        assert runner.terms == env.plugin.capabilities.terms


class TestFollowUp:
    def test_a_comment_reopens_the_published_branch_and_pushes_onto_it(
        self, env: BranchEnv
    ) -> None:
        seeded = env.seed_published_branch()
        env.publish("main", {"CHANGELOG.md": "v2\n"})
        runner = _Runner({"src/extra.md": "follow-up edit\n"})

        task = env.run(runner, TriggerKind.COMMENT_COMMAND)

        assert task.status is TaskStatus.SUCCEEDED
        assert task.pr_number is None
        work = env.work()
        assert work.closed_reason == "branch_published"
        assert work.work_branch == BRANCH
        assert work.attempts == 0  # a follow-up is not a block retry

        # The earlier delivery is kept: the new commit sits on top of it,
        # with the base merged in by the harness.
        assert env.remote_contains(seeded, BRANCH)
        assert env.remote_contains("main", BRANCH)

        # The agent is told what the branch is and what the harness did.
        assert "already published for review" in runner.branch_status
        assert "merged `origin/main` into this branch cleanly" in runner.branch_status
        assert f"with no {env.plugin.capabilities.terms.pr} on it" in runner.branch_status
        assert runner.resume_summary == EARLIER_SUMMARY

        initial, delivered = env.comments()
        assert "update its published branch" in initial
        assert f"updated branch `{BRANCH}` in response to the latest comment" in delivered
        assert "Merged `main` into the branch." in delivered
        assert COMPARE_URL in delivered
        assert "still reports conflicts" not in delivered
        # Labelled at the first delivery; a follow-up doesn't relabel.
        assert (ISSUES_REPO, 42) not in env.plugin.forge.labels

    def test_a_base_that_moves_during_the_run_is_reported_as_conflicting(
        self, env: BranchEnv
    ) -> None:
        """No PR to ask: the workspace fetches the base again after the push
        and `git merge-tree` finds the conflict."""
        env.seed_published_branch()
        runner = _Runner(
            {"src/extra.md": "follow-up edit\n"},
            during=lambda: env.publish("main", {"docs.md": BASE_ADVANCED_DOCS}),
        )

        task = env.run(runner, TriggerKind.COMMENT_COMMAND)

        assert task.status is TaskStatus.SUCCEEDED
        delivered = env.comments()[-1]
        assert "still reports conflicts between the branch and `main`" in delivered

    def test_an_unresolved_base_merge_keeps_the_branch_followable(
        self, env: BranchEnv
    ) -> None:
        seeded = env.seed_published_branch()
        env.publish("main", {"docs.md": BASE_ADVANCED_DOCS})

        # The agent leaves the conflict markers in place.
        task = env.run(_Runner(), TriggerKind.COMMENT_COMMAND)

        assert task.status is TaskStatus.ABORTED_POLICY
        assert task.error == "merge_conflict_unresolved"
        # Back to `branch_published`, so the next comment reopens the same
        # branch instead of restarting from the base over it.
        assert env.work().closed_reason == "branch_published"
        assert _git(env.remote, "rev-parse", BRANCH) == seeded
