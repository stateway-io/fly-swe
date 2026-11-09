"""The CI monitor end to end: the agent opens a PR, the forge's CI fails,
the harness hands the failure log to a fix attempt that pushes onto the same
PR, and the next sweep reports the passing CI on the PR and the issue.

Git runs for real (a bare remote plus fresh clones); the forge is the
in-memory ``FakeProviderWithCI``, and the agent a stub that edits files.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.core.models import Author, CICheck, CIStatus
from issue_agent.observability.ci_monitor import run_ci_monitor
from issue_agent.providers.registry import ProviderRegistry
from issue_agent.queue.models import Task, TaskStatus, TriggerKind
from issue_agent.queue.store import SQLiteStore
from issue_agent.runner.executor import AgentResult, run_task
from tests.fakes.fake_provider import FakeProviderWithCI
from tests.helpers.factories import make_author, make_issue, make_trigger_event
from tests.helpers.settings import github_providers

ISSUES_REPO = "stateway-io/stateway-issues"
TARGET_REPO = "stateway-io/stateway-agent-fixtures"
KEY = ("github", ISSUES_REPO, 42)
PR = 100


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file=None,
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


class _Runner:
    """Writes ``files`` into the workspace and records the handoff note."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.resume_summary: str | None = None

    def run(self, workspace_path: str, task: Task, settings: Settings, route=None,
            resume_summary=None, on_progress=None, toolchain_env=None, branch_status=None,
            comment_history=None, terms=None) -> AgentResult:  # fmt: skip
        self.resume_summary = resume_summary
        for rel, content in self.files.items():
            (Path(workspace_path) / rel).write_text(content)
        return AgentResult(success=True, cost_usd=0.1, iterations=2, submission="Fixed it.")


class Env:
    def __init__(self, tmp_path: Path, **settings_overrides) -> None:
        self.tmp = tmp_path
        self.store = SQLiteStore(":memory:")
        self.store.init()
        fields: dict = dict(
            PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
            LLM_API_KEY="k",
            DATA_DIR=str(tmp_path / "data"),
            CI_MONITOR_ENABLED=True,
            REPO_ROUTES=[{"issues": ISSUES_REPO, "target": TARGET_REPO}],
        )
        fields.update(settings_overrides)
        self.settings = SettingsNoEnv(**fields)
        self.plugin = FakeProviderWithCI("github", secret="s")
        self.registry = ProviderRegistry({"github": self.plugin})
        self.remote = tmp_path / "remote.git"
        self.remote.mkdir()
        _git(self.remote, "init", "--bare", "-b", "main")
        seed = self._clone_empty()
        (seed / "app.py").write_text("print('hi')\n")
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

    def _clone(self) -> Path:
        dest = self.tmp / f"ws-{uuid.uuid4().hex[:6]}"
        _git(self.tmp, "clone", str(self.remote), str(dest))
        _configure(dest)
        return dest

    def enqueue_issue(self) -> None:
        trigger = make_trigger_event(
            TriggerKind.LABEL_ADDED,
            issue=make_issue(repo=ISSUES_REPO, number=42, title="Fix checkout", author=Author()),
            actor=make_author(login="maintainer"),
        )
        self.store.insert_task(
            Task(
                delivery_id="delivery-1",
                repo=ISSUES_REPO,
                issue_number=42,
                trigger_kind=TriggerKind.LABEL_ADDED,
                trigger_json=trigger.model_dump_json(),
                target_repo=TARGET_REPO,
                base_branch="main",
                workflow="trunk_based",
            )
        )

    def run_next(self, runner: _Runner) -> Task:
        fetched = self.store.fetch_next()
        assert fetched is not None
        with (
            patch("issue_agent.runner.executor.Workspace") as mock_ws_cls,
            patch("issue_agent.runner.executor._resolve_agent_runner", return_value=runner),
        ):
            mock_ws_cls.return_value.clone.return_value = self._clone()
            run_task(self.store, fetched, self.settings, self.plugin)
        updated = self.store.get_task(fetched.id)
        assert updated is not None
        return updated

    def sweep(self):
        return run_ci_monitor(self.store, self.settings, self.registry)

    def set_ci(self, sha: str, *conclusions: tuple[str, str]) -> None:
        checks = [CICheck(name=n, conclusion=c, url=f"https://ci/{n}") for n, c in conclusions]
        state = "failure" if any(c == "failure" for _, c in conclusions) else "success"
        self.plugin.forge.ci_statuses[(TARGET_REPO, sha)] = CIStatus(state=state, checks=checks)

    def pr_comments(self) -> list[str]:
        return self.plugin.forge.posted(TARGET_REPO, PR)

    def issue_comments(self) -> list[str]:
        return self.plugin.forge.posted(ISSUES_REPO, 42)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_failure_is_fixed_on_the_same_pr_then_reported_green(env: Env) -> None:
    env.enqueue_issue()
    first = env.run_next(_Runner({"app.py": "print('fixed')\n"}))
    assert first.status is TaskStatus.SUCCEEDED
    assert first.pr_number == PR

    watch = env.store.get_ci_watch(*KEY)
    assert watch is not None
    assert watch.state == "watching"
    first_sha = watch.head_sha
    assert first_sha == _git(env.remote, "rev-parse", watch_branch(env))

    # The forge's full suite fails on the pushed commit.
    env.set_ci(first_sha, ("unit", "success"), ("e2e", "failure"))
    env.plugin.forge.ci_logs[(TARGET_REPO, "e2e")] = "FAILED tests/e2e/test_checkout.py"
    assert env.sweep().fix_enqueued == 1
    assert "starting a fix (attempt 2 of 3)" in env.pr_comments()[-1]

    # The fix attempt resumes on the same branch with the log in its prompt.
    fixer = _Runner({"app.py": "print('fixed twice')\n"})
    second = env.run_next(fixer)
    assert second.trigger_kind is TriggerKind.CI_FAILED
    assert second.status is TaskStatus.SUCCEEDED
    assert second.pr_number == PR
    assert fixer.resume_summary is not None
    assert "FAILED tests/e2e/test_checkout.py" in fixer.resume_summary
    assert "untrusted output" in fixer.resume_summary
    assert env.plugin.forge.prs[TARGET_REPO][0].number == PR
    assert len(env.plugin.forge.prs[TARGET_REPO]) == 1
    assert any("fix the failing CI on PR #100" in c for c in env.issue_comments())

    # The new push is watched afresh, and its CI passes.
    watch = env.store.get_ci_watch(*KEY)
    assert watch.state == "watching"
    assert watch.head_sha != first_sha
    assert watch.last_task_id == second.id
    env.set_ci(watch.head_sha, ("unit", "success"), ("e2e", "success"))
    assert env.sweep().passed == 1

    assert env.store.get_ci_watch(*KEY).state == "passed"
    assert "passed on commit" in env.pr_comments()[-1]
    assert f"passed on {TARGET_REPO}!100" in env.issue_comments()[-1]
    work = env.store.get_issue_work(*KEY)
    assert (work.state, work.closed_reason, work.attempts) == ("closed", "pr_opened", 1)


def test_failures_stop_at_the_attempts_limit(tmp_path: Path) -> None:
    env = Env(tmp_path, RESUME_MAX_ATTEMPTS=2)
    env.enqueue_issue()
    env.run_next(_Runner({"app.py": "print('one')\n"}))

    env.set_ci(env.store.get_ci_watch(*KEY).head_sha, ("e2e", "failure"))
    assert env.sweep().fix_enqueued == 1
    env.run_next(_Runner({"app.py": "print('two')\n"}))

    env.set_ci(env.store.get_ci_watch(*KEY).head_sha, ("e2e", "failure"))
    result = env.sweep()

    assert result.exhausted == 1
    assert env.store.fetch_next() is None
    assert "will not try again" in env.pr_comments()[-1]
    assert "A human needs to take it from here" in env.issue_comments()[-1]


def test_disabled_monitor_watches_nothing(tmp_path: Path) -> None:
    env = Env(tmp_path, CI_MONITOR_ENABLED=False)
    env.enqueue_issue()

    task = env.run_next(_Runner({"app.py": "print('x')\n"}))

    assert task.status is TaskStatus.SUCCEEDED
    assert env.store.get_ci_watch(*KEY) is None


def watch_branch(env: Env) -> str:
    work = env.store.get_issue_work(*KEY)
    assert work is not None
    return work.work_branch
