from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.providers.github.client import GitHubClient
from issue_agent.queue.models import Task, TaskStatus, TriggerKind
from issue_agent.queue.store import SQLiteStore
from issue_agent.runner.executor import MiniSWERunner, run_task
from issue_agent.runner.executor import _run_git as _real_run_git
from tests.helpers.factories import make_author, make_issue, make_trigger_event
from tests.helpers.fake_model import FakeModel, _make_bash_response
from tests.helpers.github import GITHUB_TERMS as TERMS
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
        DATA_DIR="/tmp/test-fake-llm-data",
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
        delivery_id="delivery-fake-1",
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


@pytest.fixture(autouse=True)
def _cleanup_data_dir() -> None:
    yield
    import shutil

    shutil.rmtree("/tmp/test-fake-llm-data", ignore_errors=True)


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
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test Bot"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("# Stateway\n\nWelcome.")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    bare = tmp_path / "remote.git"
    bare.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare"], cwd=bare, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


@pytest.fixture
def pr_mock() -> MagicMock:
    pr = MagicMock()
    pr.number = 201
    pr.html_url = "https://github.com/stateway-io/stateway-agent-fixtures/pull/201"
    pr.head.ref = "agent/issue-42-fix"
    return pr


def _make_local_git_repo(tmp_path: Path, name: str) -> Path:
    """Same recipe as the `git_workspace` fixture above, factored out so a
    single test can spin up more than one independent local repo (one per
    RepoRoute pair) — Workspace.clone() is mocked in these tests to return
    whichever of these paths corresponds to the task being processed."""
    repo = tmp_path / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "bot@test.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test Bot"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text(f"# {name}\n\nWelcome.")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


# ── Scenario 1: Full success flow ────────────────────────────────────


class TestFullFlow:
    """Real DefaultAgent + FakeModel producing edits through to PR."""

    def test_agent_edits_flow_to_success(
        self,
        store: SQLiteStore,
        settings: Settings,
        git_workspace: Path,
        pr_mock: MagicMock,
    ) -> None:
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        fake = FakeModel(
            responses=[
                _make_bash_response("echo 'fixed by agent' >> README.md"),
                _make_bash_response("cat README.md"),
                _make_bash_response("printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\ndone\\n'"),
            ],
            cost_per_call=0.01,
        )
        runner = MiniSWERunner(model_override=fake)

        with (
            patch("issue_agent.runner.executor.Workspace") as mock_ws_cls,
            patch("issue_agent.runner.executor._resolve_agent_runner", return_value=runner),
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
        assert updated.cost_usd >= 0.01
        assert updated.iterations > 0

        readme = git_workspace / "README.md"
        assert "fixed by agent" in readme.read_text()

        # The trajectory must be persisted under <data_dir>/events/<task_id>/.
        trajectory_path = Path(settings.data_dir) / "events" / task.id / "trajectory.json"
        assert trajectory_path.is_file()
        trajectory = json.loads(trajectory_path.read_text())
        assert trajectory["info"]["exit_status"] == "Submitted"

    def test_no_diff_produces_aborted_policy(
        self,
        store: SQLiteStore,
        settings: Settings,
        git_workspace: Path,
    ) -> None:
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        fake = FakeModel(
            responses=[
                _make_bash_response("echo 'thinking...'"),
                _make_bash_response("printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\ndone\\n'"),
            ],
            cost_per_call=0.01,
        )
        runner = MiniSWERunner(model_override=fake)

        with (
            patch("issue_agent.runner.executor.Workspace") as mock_ws_cls,
            patch("issue_agent.runner.executor._resolve_agent_runner", return_value=runner),
            patch.object(GitHubClient, "create_comment") as mock_create_comment,
        ):
            mock_ws = MagicMock()
            mock_ws.clone.return_value = git_workspace
            mock_ws_cls.return_value = mock_ws

            run_task(store, fetched, settings, stub_github_plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.ABORTED_POLICY

        # initial "started work" comment + the outcome comment
        assert mock_create_comment.call_count == 2
        comment_body = mock_create_comment.call_args[0][2]
        assert "no changes" in comment_body.lower()


# ── Multi-repo, multi-workflow acceptance ──
#
# The PRD's acceptance criterion for the whole multi-repo/git-flow
# migration was "two issues in distinct repos, with distinct workflows,
# produce correct PRs against the correct targets" — but no integration
# test ever configured more than one RepoRoute, and none exercised gitflow
# at all. This proves the end-to-end claim, not just the pieces of it.


class TestMultiRepoMultiWorkflowIntegration:
    def test_two_repos_two_workflows_produce_correct_prs(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
        settings = _make_settings(
            DATA_DIR=str(tmp_path / "data"),
            REPO_ROUTES=[
                {
                    # trunk_based pair — workflow/base_branch/branch_prefix
                    # left at their defaults (trunk_based, "main", "agent").
                    "issues": "org-trunk/issues",
                    "target": "org-trunk/code",
                },
                {
                    "issues": "org-flow/issues",
                    "target": "org-flow/code",
                    "workflow": "gitflow",
                    "base_branch": "dev",
                },
            ],
        )

        trunk_repo = _make_local_git_repo(tmp_path, "trunk-local")
        flow_repo = _make_local_git_repo(tmp_path, "flow-local")

        def _trigger_json(repo: str, number: int, title: str) -> str:
            return make_trigger_event(
                issue=make_issue(repo=repo, number=number, title=title, body="x", labels=["docs"]),
                actor=make_author(login="testuser"),
            ).model_dump_json()

        trunk_task = _make_task(
            delivery_id="d-trunk",
            repo="org-trunk/issues",
            issue_number=7,
            trigger_json=_trigger_json("org-trunk/issues", 7, "Fix the trunk based thing"),
            target_repo="org-trunk/code",
            base_branch="main",
            workflow="trunk_based",
        )
        flow_task = _make_task(
            delivery_id="d-flow",
            repo="org-flow/issues",
            issue_number=9,
            trigger_json=_trigger_json("org-flow/issues", 9, "Fix the gitflow thing"),
            target_repo="org-flow/code",
            base_branch="dev",
            workflow="gitflow",
        )
        store.insert_task(trunk_task)
        store.insert_task(flow_task)

        def _run(task: Task, ws_path: Path, pr_number: int) -> MagicMock:
            fake = FakeModel(
                responses=[
                    _make_bash_response("echo 'fixed by agent' >> README.md"),
                    _make_bash_response("printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\ndone\\n'"),
                ],
                cost_per_call=0.01,
            )
            runner = MiniSWERunner(model_override=fake)
            pr_result = MagicMock()
            pr_result.number = pr_number
            pr_result.html_url = f"https://github.com/{task.target_repo}/pull/{pr_number}"
            pr_result.head.ref = "agent/branch"

            with (
                patch("issue_agent.runner.executor.Workspace") as mock_ws_cls,
                patch(
                    "issue_agent.runner.executor._resolve_agent_runner",
                    return_value=runner,
                ),
                patch("issue_agent.runner.executor._run_git") as mock_git,
                patch.object(GitHubClient, "create_pr", return_value=pr_result) as mock_create_pr,
                patch.object(GitHubClient, "create_comment"),
                patch.object(GitHubClient, "find_pr_by_branch", return_value=None),
                patch.object(GitHubClient, "get_repo") as mock_get_repo,
            ):
                mock_get_repo.return_value.get_issue.return_value.add_to_labels = MagicMock()

                mock_ws = MagicMock()
                mock_ws.clone.return_value = ws_path
                mock_ws_cls.return_value = mock_ws

                def _selective_git(*args, **kwargs):
                    if args and len(args) > 1 and args[1] in ("remote", "push"):
                        return ""
                    return _real_run_git(*args, **kwargs)

                mock_git.side_effect = _selective_git

                run_task(store, task, settings, stub_github_plugin(settings))

            return mock_create_pr

        mock_create_pr_trunk = _run(trunk_task, trunk_repo, pr_number=301)
        mock_create_pr_flow = _run(flow_task, flow_repo, pr_number=302)

        updated_trunk = store.get_task(trunk_task.id)
        updated_flow = store.get_task(flow_task.id)
        assert updated_trunk is not None
        assert updated_flow is not None
        assert updated_trunk.status is TaskStatus.SUCCEEDED
        assert updated_flow.status is TaskStatus.SUCCEEDED
        assert updated_trunk.pr_number == 301
        assert updated_flow.pr_number == 302

        # trunk_based pair: PR opened against `main`, branch
        # `<branch_prefix>/issue-<n>-<slug>`, targeting its own repo.
        trunk_call = mock_create_pr_trunk.call_args.kwargs
        assert trunk_call["full_name"] == "org-trunk/code"
        assert trunk_call["base"] == "main"
        assert trunk_call["head"].startswith("agent/issue-7-")
        assert "fix-the-trunk-based-thing" in trunk_call["head"]

        # gitflow pair: PR opened against `dev`, branch `<base_branch>-<slug>`
        # (no issue number in the branch), targeting its OWN distinct repo —
        # not the trunk_based pair's.
        flow_call = mock_create_pr_flow.call_args.kwargs
        assert flow_call["full_name"] == "org-flow/code"
        assert flow_call["base"] == "dev"
        assert flow_call["head"] == "dev-fix-the-gitflow-thing"
        assert not flow_call["head"].startswith("agent/issue-")

        # The two pairs' target repos are clearly distinct from each other
        # (and from either pair's own issue repo).
        assert trunk_call["full_name"] != flow_call["full_name"]
        assert trunk_call["full_name"] not in (trunk_task.repo, flow_task.repo)
        assert flow_call["full_name"] not in (trunk_task.repo, flow_task.repo)

        readme_trunk = trunk_repo / "README.md"
        readme_flow = flow_repo / "README.md"
        assert "fixed by agent" in readme_trunk.read_text()
        assert "fixed by agent" in readme_flow.read_text()


# ── Scenario 4: Task timeout via hanging FakeModel ───────────────────


class TestTimeoutCap:
    """FakeModel that hangs → TASK_TIMEOUT_SECONDS → ABORTED_BUDGET."""

    def test_hanging_model_triggers_timeout(
        self,
        store: SQLiteStore,
        settings: Settings,
        git_workspace: Path,
    ) -> None:
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        import time

        def _slow_query(self_obj: FakeModel, messages: list) -> None:
            # The native wall_time_limit_seconds check only runs *between*
            # steps (before the next model call), so it can't preempt a
            # single in-flight call — the sleep just needs to clear
            # TASK_TIMEOUT_SECONDS=1 by the time the *next* query() starts.
            time.sleep(2)

        fake = FakeModel(
            responses=[_make_bash_response("echo 'will hang'")],
            cost_per_call=0.01,
            query_hook=_slow_query,
        )
        runner = MiniSWERunner(model_override=fake)

        timeout_settings = _make_settings(
            DATA_DIR=str(settings.data_dir),
            TASK_TIMEOUT_SECONDS=1,
        )

        with (
            patch("issue_agent.runner.executor.Workspace") as mock_ws_cls,
            patch("issue_agent.runner.executor._resolve_agent_runner", return_value=runner),
            patch.object(GitHubClient, "create_comment") as mock_create_comment,
        ):
            mock_ws = MagicMock()
            mock_ws.clone.return_value = git_workspace
            mock_ws_cls.return_value = mock_ws

            run_task(store, fetched, timeout_settings, stub_github_plugin(timeout_settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.ABORTED_BUDGET
        assert "timeout" in (updated.error or "")

        # initial "started work" comment + the outcome comment
        assert mock_create_comment.call_count == 2
        comment_body = mock_create_comment.call_args[0][2]
        assert "maximum" in comment_body.lower()


# ── Scenarios 2 & 3: Budget and iteration caps at MiniSWERunner level ─


class TestAgentLevelCaps:
    """Caps enforced natively by DefaultAgent (step_limit, cost_limit).
    These tests use the real MiniSWERunner + DefaultAgent with a FakeModel
    injected, exercising the real cap enforcement code path.
    """

    def test_cost_limit_stops_agent(self) -> None:
        fake = FakeModel(
            responses=[
                _make_bash_response("echo 'costly work' >> /dev/null"),
            ],
            cost_per_call=0.03,
        )

        settings = _make_settings(MAX_BUDGET_PER_TASK_USD=0.05, MAX_ITERATIONS=100)
        runner = MiniSWERunner(model_override=fake)
        task = _make_task()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None

        result = runner.run("/tmp/ws", task, settings, route, terms=TERMS)

        assert result.cost_usd >= 0.05, f"Expected cost >= 0.05, got {result.cost_usd}"
        assert result.iterations == 2, (
            f"Expected 2 iterations (cost 0.06 >= 0.05 limit on 3rd call), got {result.iterations}"
        )
        # Hitting cost_limit must not be reported as a successful run.
        # Only cost_limit is exceeded here (MAX_ITERATIONS=100 is nowhere
        # close), so the error must say "cost", not the ambiguous generic
        # label — the reverse case (only step_limit hit) was once
        # misreported as "budget exceeded".
        assert result.success is False
        assert result.error == "cost_limit_exceeded"

    def test_step_limit_stops_agent(self) -> None:
        fake = FakeModel(
            responses=[_make_bash_response("echo 'loop'")],
            cost_per_call=0.01,
        )
        settings = _make_settings(MAX_ITERATIONS=3, MAX_BUDGET_PER_TASK_USD=10.0)

        runner = MiniSWERunner(model_override=fake)
        task = _make_task()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None
        result = runner.run("/tmp/ws", task, settings, route, terms=TERMS)

        assert result.iterations <= 3, f"Expected ≤3 iterations, got {result.iterations}"
        assert result.cost_usd >= 0.0
        # Hitting step_limit must not be reported as a successful run.
        # Only step_limit is exceeded here (MAX_BUDGET_PER_TASK_USD=10.0 is
        # nowhere close), so the error must say "iteration", not the
        # ambiguous generic label.
        assert result.success is False
        assert result.error == "iteration_limit_exceeded"
