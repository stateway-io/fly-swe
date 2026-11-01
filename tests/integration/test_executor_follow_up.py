"""Characterization tests for the executor's PR follow-up / base-merge flow.

A maintainer comments on an issue whose PR is already open: the work item is
reopened from ``pr_opened``, the existing remote branch is checked out, the
harness merges ``origin/<base>`` into it, the agent runs, and the result is
force-pushed onto the same PR.

These pin what ``run_task`` does today — not what it should do — so the
provider refactor can move the GitHub calls behind an interface without
changing behavior. Git runs for real (a bare remote plus fresh clones); only
the ``GitHubClient`` methods are mocked, and all of that wiring lives in
``FollowUpEnv.run`` so a later change of patch targets touches one place.
"""

from __future__ import annotations

import base64
import itertools
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.core.models import Author
from issue_agent.providers.github.auth import GitHubAppAuth
from issue_agent.providers.github.client import GitHubClient
from issue_agent.providers.github.normalize import bot_identity_from_settings
from issue_agent.providers.github.plugin import GitHubProviderPlugin
from issue_agent.queue.models import IssueWork, Task, TaskStatus, TriggerKind
from issue_agent.queue.store import SQLiteStore
from issue_agent.runner.executor import AgentResult, run_task
from issue_agent.runner.executor import _run_git as _real_run_git
from tests.helpers.factories import make_author, make_issue, make_trigger_event
from tests.helpers.settings import github_providers, with_github

ISSUES_REPO = "stateway-io/stateway-issues"
TARGET_REPO = "stateway-io/stateway-agent-fixtures"
BRANCH = "agent/issue-42-fix-broken-link"
PR_NUMBER = 201
EARLIER_SUMMARY = "Earlier summary of the fix."

BASE_DOCS = "alpha\nbeta\ngamma\n"


def _pr_url(number: int, host: str) -> str:
    """The ``html_url`` GitHub reports for a PR on ``host``."""
    return f"{host}/{TARGET_REPO}/pull/{number}"


BRANCH_DOCS = "alpha\nbeta-branch\ngamma\n"
BASE_ADVANCED_DOCS = "alpha\nbeta-main\ngamma\n"  # conflicts with BRANCH_DOCS
RESOLVED_DOCS = "alpha\nbeta-resolved\ngamma\n"


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)
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
    """Writes ``edit`` into the workspace and records what the harness handed
    it. An empty ``edit`` is an agent that changes nothing."""

    def __init__(self, edit: dict[str, str] | None = None) -> None:
        self.edit = edit or {}
        self.branch_status: str | None = None
        self.resume_summary: str | None = None

    def run(
        self,
        workspace_path: str,
        task: Task,
        settings: Settings,
        route=None,
        resume_summary=None,
        on_progress=None,
        toolchain_env=None,
        branch_status=None,
        comment_history=None,
        terms=None,
    ) -> AgentResult:
        self.branch_status = branch_status
        self.terms = terms
        self.resume_summary = resume_summary
        _write(Path(workspace_path), self.edit)
        return AgentResult(success=True, cost_usd=0.2, iterations=3)


@dataclass
class Outcome:
    task_id: str
    runner: _Runner
    create_pr: MagicMock
    create_comment: MagicMock
    get_pull_request: MagicMock
    git_calls: list[tuple[tuple[str, ...], dict[str, str]]]

    def issue_comments(self) -> list[str]:
        return [
            c.args[2] for c in self.create_comment.call_args_list if c.args[:2] == (ISSUES_REPO, 42)
        ]

    def pr_comments(self, pr_number: int = PR_NUMBER) -> list[str]:
        return [
            c.args[2]
            for c in self.create_comment.call_args_list
            if c.args[:2] == (TARGET_REPO, pr_number)
        ]

    def authenticated_git(self, command: str) -> list[dict[str, str]]:
        """The env of every ``git <command>`` the run authenticated."""
        return [
            env for args, env in self.git_calls if args[0] == command and "GIT_CONFIG_KEY_0" in env
        ]


class FollowUpEnv:
    def __init__(self, tmp_path: Path, store: SQLiteStore, settings: Settings) -> None:
        self.tmp = tmp_path
        self.store = store
        self.settings = settings
        self.remote = tmp_path / "remote.git"
        self.remote.mkdir()
        _git(self.remote, "init", "--bare", "-b", "main")
        seed = tmp_path / "seed"
        seed.mkdir()
        _git(seed, "init", "-b", "main")
        _configure(seed)
        _write(seed, {"docs.md": BASE_DOCS})
        _git(seed, "add", "-A")
        _git(seed, "commit", "-m", "initial commit")
        _git(seed, "remote", "add", "origin", str(self.remote))
        _git(seed, "push", "origin", "main")

    # -- remote state -----------------------------------------------------

    def _clone(self, name: str) -> Path:
        dest = self.tmp / f"{name}-{uuid.uuid4().hex[:6]}"
        _git(self.tmp, "clone", str(self.remote), str(dest))
        _configure(dest)
        return dest

    def publish(self, branch: str, files: dict[str, str], start: str = "main") -> str:
        """Commit ``files`` on top of ``origin/<start>`` and push as ``branch``."""
        clone = self._clone("pub")
        _git(clone, "checkout", "-B", branch, f"origin/{start}")
        _write(clone, files)
        _git(clone, "add", "-A")
        _git(clone, "commit", "-m", f"update {branch}")
        _git(clone, "push", "-f", "origin", branch)
        return _git(clone, "rev-parse", "HEAD")

    def replace_base_with_unrelated_history(self) -> None:
        orphan = self.tmp / "orphan"
        orphan.mkdir()
        _git(orphan, "init", "-b", "main")
        _configure(orphan)
        _write(orphan, {"README.md": "rewritten\n"})
        _git(orphan, "add", "-A")
        _git(orphan, "commit", "-m", "history rewritten")
        _git(orphan, "push", "-f", str(self.remote), "main")

    def remote_sha(self, ref: str) -> str:
        return _git(self.remote, "rev-parse", ref)

    def remote_file(self, ref: str, path: str) -> str:
        return _git(self.remote, "show", f"{ref}:{path}")

    def remote_has(self, ref: str, path: str) -> bool:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{ref}:{path}"], cwd=self.remote, capture_output=True
        )
        return result.returncode == 0

    def remote_parents(self, ref: str) -> list[str]:
        return _git(self.remote, "rev-list", "--parents", "-n1", ref).split()[1:]

    def remote_contains(self, ancestor: str, ref: str) -> bool:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, ref],
            cwd=self.remote,
            capture_output=True,
        )
        return result.returncode == 0

    # -- issue_work -------------------------------------------------------

    def seed_pr_opened_work(self) -> None:
        self.store.open_issue_work("github", ISSUES_REPO, 42, TARGET_REPO, "main", BRANCH)
        self.store.update_issue_work(
            "github",
            ISSUES_REPO,
            42,
            state="closed",
            closed_reason="pr_opened",
            pr_number=PR_NUMBER,
            last_summary=EARLIER_SUMMARY,
            last_summary_kind="submission",
        )

    def seed_open_pr(self) -> str:
        """An open PR whose branch carries one commit ahead of main."""
        self.seed_pr_opened_work()
        return self.publish(BRANCH, {"docs.md": BRANCH_DOCS, "src/fix.md": "fixed by agent\n"})

    def work(self) -> IssueWork:
        work = self.store.get_issue_work("github", ISSUES_REPO, 42)
        assert work is not None
        return work

    # -- one follow-up run ------------------------------------------------

    def run(
        self,
        runner: _Runner,
        *,
        pr_exists: bool = True,
        mergeable: bool | None = True,
        pull_request_raises: bool = False,
        new_pr_number: int = 305,
    ) -> Outcome:
        ws_path = self._clone("ws")
        task = Task(
            delivery_id="delivery-follow-up",
            repo=ISSUES_REPO,
            issue_number=42,
            trigger_kind=TriggerKind.COMMENT_COMMAND,
            trigger_json=make_trigger_event(
                TriggerKind.COMMENT_COMMAND,
                issue=make_issue(
                    repo=ISSUES_REPO,
                    number=42,
                    title="Fix broken link in docs",
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

        existing = (
            SimpleNamespace(
                number=PR_NUMBER,
                html_url=_pr_url(PR_NUMBER, self.settings.providers["github"].host),
                head=SimpleNamespace(ref=BRANCH, sha=""),
            )
            if pr_exists
            else None
        )

        def pull_request(repo: str, number: int):
            if pull_request_raises:
                raise RuntimeError("GitHub unavailable")
            return SimpleNamespace(
                head=SimpleNamespace(sha=self.remote_sha(BRANCH)), mergeable=mergeable
            )

        git_calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

        def recording_git(ws: Path, *args: str, env: dict[str, str] | None = None) -> str:
            git_calls.append((args, env or {}))
            return _real_run_git(ws, *args, env=env)

        tokens = itertools.count(1)
        # The real GitHub plugin over a stubbed App auth: everything below the
        # PyGithub boundary (GitHubClient) runs as in production.
        auth = MagicMock(spec=GitHubAppAuth)
        auth.installation_for.return_value = 1
        auth.token_for.side_effect = lambda *_: f"tok-{next(tokens)}"
        plugin = GitHubProviderPlugin(
            "github",
            webhook_secret="s",
            identity=bot_identity_from_settings(
                self.settings.providers["github"], self.settings.agent_name
            ),
            host=self.settings.providers["github"].host,
            label_agent_generated=self.settings.pr_label_agent_generated,
            auth_factory=lambda: auth,
        )

        with (
            patch("issue_agent.runner.executor._run_git", side_effect=recording_git),
            patch("issue_agent.runner.executor.Workspace") as mock_ws_cls,
            patch("issue_agent.runner.executor._resolve_agent_runner", return_value=runner),
            patch.object(
                GitHubClient,
                "create_pr",
                return_value=SimpleNamespace(
                    number=new_pr_number,
                    html_url=_pr_url(new_pr_number, self.settings.providers["github"].host),
                    head=SimpleNamespace(ref=BRANCH),
                ),
            ) as create_pr,
            patch.object(GitHubClient, "create_comment") as create_comment,
            patch.object(GitHubClient, "find_pr_by_branch", return_value=existing),
            patch.object(GitHubClient, "get_repo"),
            patch.object(
                GitHubClient, "get_pull_request", side_effect=pull_request
            ) as get_pull_request,
            patch("issue_agent.providers.github.pr.time.sleep"),
        ):
            mock_ws_cls.return_value.clone.return_value = ws_path
            run_task(self.store, fetched, self.settings, plugin)

        return Outcome(task.id, runner, create_pr, create_comment, get_pull_request, git_calls)

    def task_status(self, outcome: Outcome) -> Task:
        task = self.store.get_task(outcome.task_id)
        assert task is not None
        return task


@pytest.fixture
def env(tmp_path: Path) -> FollowUpEnv:
    store = SQLiteStore(":memory:")
    store.init()
    settings = SettingsNoEnv(
        PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
        LLM_API_KEY="k",
        DATA_DIR=str(tmp_path / "data"),
        REPO_ROUTES=[{"issues": ISSUES_REPO, "target": TARGET_REPO}],
    )
    return FollowUpEnv(tmp_path, store, settings)


class TestCleanBaseMerge:
    def test_base_ahead_without_conflict_is_merged_and_pushed_onto_the_same_pr(
        self, env: FollowUpEnv
    ) -> None:
        seeded = env.seed_open_pr()
        env.publish("main", {"CHANGELOG.md": "v2\n"})
        runner = _Runner({"src/extra.md": "follow-up edit\n"})

        out = env.run(runner)

        task = env.task_status(out)
        assert task.status is TaskStatus.SUCCEEDED
        assert task.pr_number == PR_NUMBER
        out.create_pr.assert_not_called()

        # The agent is told the merge went through, and which PR this is.
        assert "merged `origin/main` into this branch cleanly" in runner.branch_status
        assert f"Pull Request #{PR_NUMBER}" in runner.branch_status
        assert runner.resume_summary == EARLIER_SUMMARY

        # The PR branch now has the base's commit and the follow-up edit.
        assert env.remote_sha(BRANCH) != seeded
        assert env.remote_contains("main", BRANCH)
        assert env.remote_has(BRANCH, "CHANGELOG.md")
        assert env.remote_file(BRANCH, "src/extra.md") == "follow-up edit"

        (pr_comment,) = out.pr_comments()
        assert "updated this PR in response to a comment" in pr_comment
        assert "Merged `main` into the branch." in pr_comment
        assert "still reports conflicts" not in pr_comment
        assert any("updated the PR" in c for c in out.issue_comments())

        work = env.work()
        assert (work.state, work.closed_reason, work.pr_number) == (
            "closed",
            "pr_opened",
            PR_NUMBER,
        )
        # A human follow-up is not a block retry.
        assert work.attempts == 0


class TestConflictingBaseMerge:
    def test_agent_resolved_conflict_is_recorded_as_a_two_parent_merge(
        self, env: FollowUpEnv
    ) -> None:
        seeded = env.seed_open_pr()
        base_tip = env.publish("main", {"docs.md": BASE_ADVANCED_DOCS})
        runner = _Runner({"docs.md": RESOLVED_DOCS})

        out = env.run(runner)

        assert env.task_status(out).status is TaskStatus.SUCCEEDED
        assert "stopped with conflicts" in runner.branch_status
        assert "docs.md (both modified)" in runner.branch_status

        assert env.remote_parents(BRANCH) == [seeded, base_tip]
        assert env.remote_file(BRANCH, "docs.md") == RESOLVED_DOCS.strip()

        (pr_comment,) = out.pr_comments()
        assert "Merged `main` into the branch; conflicts resolved in `docs.md`." in pr_comment
        assert env.work().closed_reason == "pr_opened"

    def test_unresolved_conflict_pushes_nothing_and_keeps_the_pr_followable(
        self, env: FollowUpEnv
    ) -> None:
        seeded = env.seed_open_pr()
        env.publish("main", {"docs.md": BASE_ADVANCED_DOCS})

        out = env.run(_Runner())  # the agent leaves the markers in place

        task = env.task_status(out)
        assert task.status is TaskStatus.ABORTED_POLICY
        assert task.error == "merge_conflict_unresolved"
        assert env.remote_sha(BRANCH) == seeded
        out.create_pr.assert_not_called()
        assert out.pr_comments() == []
        assert any("Nothing was pushed" in c for c in out.issue_comments())

        # Back to pr_opened, so the next comment reopens the same branch.
        work = env.work()
        assert (work.state, work.closed_reason, work.pr_number) == (
            "closed",
            "pr_opened",
            PR_NUMBER,
        )

    def test_merge_that_cannot_start_is_reported_and_the_edit_is_still_pushed(
        self, env: FollowUpEnv
    ) -> None:
        # Unrelated histories: `git merge` fails with nothing unmerged, which
        # is neither "merged" nor "conflicted".
        seeded = env.seed_open_pr()
        env.replace_base_with_unrelated_history()
        runner = _Runner({"src/extra.md": "follow-up edit\n"})

        out = env.run(runner)

        assert env.task_status(out).status is TaskStatus.SUCCEEDED
        assert "could not merge" in runner.branch_status
        assert env.remote_sha(BRANCH) != seeded
        assert env.remote_has(BRANCH, "src/extra.md")
        (pr_comment,) = out.pr_comments()
        assert "⚠️ Could not merge `main` into the branch." in pr_comment


class TestFollowUpWithoutNewEdits:
    def test_branch_already_ahead_is_pushed_as_is_and_reported_on_the_pr(
        self, env: FollowUpEnv
    ) -> None:
        seeded = env.seed_open_pr()  # base did not move

        runner = _Runner()
        out = env.run(runner)

        task = env.task_status(out)
        assert task.status is TaskStatus.SUCCEEDED
        assert task.pr_number == PR_NUMBER
        out.create_pr.assert_not_called()
        assert env.remote_sha(BRANCH) == seeded
        # "Already up to date" counts as a clean merge.
        assert "merged `origin/main` into this branch cleanly" in runner.branch_status

        (pr_comment,) = out.pr_comments()
        assert "src/fix.md" in pr_comment
        assert any("updated the PR" in c for c in out.issue_comments())
        assert env.work().closed_reason == "pr_opened"


class TestPrNoLongerOpen:
    def test_closed_pr_gets_a_new_one_without_a_pr_comment(self, env: FollowUpEnv) -> None:
        env.seed_open_pr()
        runner = _Runner({"src/extra.md": "follow-up edit\n"})

        out = env.run(runner, pr_exists=False, new_pr_number=305)

        task = env.task_status(out)
        assert task.status is TaskStatus.SUCCEEDED
        assert task.pr_number == 305
        out.create_pr.assert_called_once()
        assert out.create_pr.call_args.kwargs["head"] == BRANCH
        assert out.create_pr.call_args.kwargs["base"] == "main"

        assert out.pr_comments(PR_NUMBER) == []
        assert out.pr_comments(305) == []
        assert any(f"{TARGET_REPO}#305" in c for c in out.issue_comments())
        assert not any("updated the PR" in c for c in out.issue_comments())
        assert env.work().pr_number == 305

    def test_work_branch_deleted_from_remote_restarts_from_the_base(self, env: FollowUpEnv) -> None:
        env.seed_pr_opened_work()  # row says pr_opened, but the branch is gone
        runner = _Runner({"src/extra.md": "follow-up edit\n"})

        out = env.run(runner, pr_exists=False, new_pr_number=306)

        assert env.task_status(out).status is TaskStatus.SUCCEEDED
        # No resumed branch, so no merge status — but the prior summary rides in.
        assert runner.branch_status == ""
        assert runner.resume_summary == EARLIER_SUMMARY
        assert env.remote_has(BRANCH, "src/extra.md")
        assert _git(env.remote, "rev-list", "--count", f"main..{BRANCH}") == "1"
        out.create_pr.assert_called_once()
        assert out.pr_comments(306) == []


class TestStillConflictingAfterPush:
    def test_github_reporting_conflicts_is_noted_on_the_pr_and_the_issue(
        self, env: FollowUpEnv
    ) -> None:
        env.seed_open_pr()
        out = env.run(_Runner({"src/extra.md": "follow-up edit\n"}), mergeable=False)

        assert env.task_status(out).status is TaskStatus.SUCCEEDED
        (pr_comment,) = out.pr_comments()
        assert "still reports conflicts" in pr_comment
        assert any("still reports conflicts" in c for c in out.issue_comments())

    @pytest.mark.parametrize(
        ("mergeable", "reads"),
        [(True, 1), (None, 5)],  # None: GitHub is still computing — poll, then give up
    )
    def test_no_note_unless_github_definitely_reports_conflicts(
        self, env: FollowUpEnv, mergeable: bool | None, reads: int
    ) -> None:
        env.seed_open_pr()
        out = env.run(_Runner({"src/extra.md": "follow-up edit\n"}), mergeable=mergeable)

        assert env.task_status(out).status is TaskStatus.SUCCEEDED
        assert out.get_pull_request.call_count == reads
        assert not any("still reports conflicts" in c for c in out.pr_comments())
        assert not any("still reports conflicts" in c for c in out.issue_comments())

    def test_pull_request_lookup_failure_does_not_fail_the_run(self, env: FollowUpEnv) -> None:
        env.seed_open_pr()
        out = env.run(_Runner({"src/extra.md": "follow-up edit\n"}), pull_request_raises=True)

        assert env.task_status(out).status is TaskStatus.SUCCEEDED
        assert out.get_pull_request.call_count == 1
        assert not any("still reports conflicts" in c for c in out.pr_comments())


class TestGitHubEnterpriseHost:
    HOST = "https://ghe.example.com"

    @staticmethod
    def _token(git_env: dict[str, str]) -> str:
        header = git_env["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")
        return base64.b64decode(header).decode().removeprefix("x-access-token:")

    def test_every_authenticated_git_call_is_scoped_to_the_configured_host(
        self, env: FollowUpEnv
    ) -> None:
        env.settings = with_github(env.settings, host=self.HOST)
        env.seed_open_pr()

        out = env.run(_Runner({"src/extra.md": "follow-up edit\n"}))

        assert env.task_status(out).status is TaskStatus.SUCCEEDED
        key = f"http.{self.HOST}.extraHeader"
        for command in ("ls-remote", "fetch", "push"):
            calls = out.authenticated_git(command)
            assert calls, f"git {command} ran without credentials"
            assert all(call["GIT_CONFIG_KEY_0"] == key for call in calls)

    def test_push_authenticates_with_a_token_fetched_after_the_agent_ran(
        self, env: FollowUpEnv
    ) -> None:
        env.seed_open_pr()

        out = env.run(_Runner({"src/extra.md": "follow-up edit\n"}))

        (pushed_with,) = {self._token(e) for e in out.authenticated_git("push")}
        read_with = {self._token(e) for e in out.authenticated_git("ls-remote")}
        assert pushed_with not in read_with

    def test_issue_comment_links_the_pr_on_the_configured_host(self, env: FollowUpEnv) -> None:
        env.settings = with_github(env.settings, host=self.HOST)
        env.seed_open_pr()

        out = env.run(_Runner({"src/extra.md": "follow-up edit\n"}))

        link = f"[{TARGET_REPO}#{PR_NUMBER}]({self.HOST}/{TARGET_REPO}/pull/{PR_NUMBER})"
        assert any(link in c for c in out.issue_comments())
        assert not any("https://github.com" in c for c in out.issue_comments())
