from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.core.models import PRRef
from issue_agent.providers.base import ForgeClient, ProviderPlugin, PullRequestClient
from issue_agent.providers.github.auth import GitHubAppAuth
from issue_agent.providers.github.plugin import GitHubProviderPlugin
from issue_agent.providers.github.transport import GitHubTransport
from issue_agent.queue.models import IssueWork, Task, TaskStatus, TriggerKind
from issue_agent.queue.store import SQLiteStore
from issue_agent.runner.executor import (
    CHECKS_SENTINEL_FILENAME,
    TEST_COMMAND_SENTINEL_FILENAME,
    AgentResult,
    BaseUpdate,
    ReportedCheck,
    WipOutcome,
    _build_abort_comment,
    _build_fix_comment,
    _build_initial_comment,
    _build_pr_update_comment,
    _build_testing_notes,
    _consume_checks_sentinel,
    _consume_test_command_sentinel,
    _extract_author_login,
    _extract_issue_title,
    _parse_git_status,
    _prepare_toolchain,
    _resolve_pr_description,
    _resolve_test_command,
    _run_test_command,
    run_task,
)
from issue_agent.runner.llm_errors import ProviderError
from tests.fakes.fake_provider import FakeProvider, FakeProviderWithPulls
from tests.helpers.factories import (
    make_author,
    make_comment,
    make_issue,
    make_trigger_event,
)
from tests.helpers.github import GITHUB_TERMS as TERMS
from tests.helpers.settings import github_providers, with_github


@pytest.fixture(autouse=True)
def _stub_branch_head_and_integrity():
    """`_run_git` is mocked with positional side_effect lists in this module;
    keep the start-sha lookup and the pre-push integrity guard out of those
    lists (both have their own tests below)."""
    with (
        patch("issue_agent.runner.executor._head_sha", return_value="start-sha"),
        patch("issue_agent.runner.executor._assert_branch_integrity"),
    ):
        yield


_ISSUE = make_issue(
    number=42,
    title="Fix broken link in docs",
    body="The README has a broken link.",
    labels=["docs"],
)
_TRIGGER = make_trigger_event(issue=_ISSUE, actor=make_author(login="testuser"))


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


def _make_settings(**overrides: dict) -> Settings:
    # TEST_COMMAND is a convenience kwarg that folds into REPO_ROUTES[0]'s
    # project.test_command so call sites can override it without spelling
    # out a full repo_routes entry.
    test_command = overrides.pop("TEST_COMMAND", None)
    repo_routes = overrides.pop("REPO_ROUTES", None)
    if repo_routes is None:
        entry: dict = {
            "issues": "stateway-io/stateway-issues",
            "target": "stateway-io/stateway-agent-fixtures",
        }
        if test_command is not None:
            entry["project"] = {"test_command": test_command}
        repo_routes = [entry]

    defaults: dict = dict(
        PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
        LLM_API_KEY="k",
        DATA_DIR="/tmp/test-executor-data",
        REPO_ROUTES=repo_routes,
    )
    defaults.update(overrides)
    return SettingsNoEnv(**defaults)


_FROZEN_BRANCH = "agent/issue-42-fix-broken-link-in-docs"


_PR_URL = "https://github.com/stateway-io/stateway-agent-fixtures/pull/101"


def _pr_ref(number: int, repo: str, host: str = "https://github.com") -> PRRef:
    return PRRef(number=number, url=f"{host}/{repo}/pull/{number}", head_ref="b")


class _GitHub:
    """The task's provider as ``run_task`` sees it: GitHub's real
    capabilities and transport (over a stubbed App auth, token ``tok``),
    with the issue and PR clients as mocks the tests inspect."""

    def __init__(self) -> None:
        self.auth = MagicMock(spec=GitHubAppAuth)
        self.auth.token_for.return_value = "tok"
        self.forge = MagicMock(spec=ForgeClient)
        self.forge.list_comments.return_value = []
        self.forge.get_default_branch.return_value = "main"
        self.pulls = MagicMock(spec=PullRequestClient)
        self.pulls.find_open_pr.return_value = None
        self.pulls.create_pr.return_value = PRRef(number=101, url=_PR_URL, head_ref=_FROZEN_BRANCH)
        self.pulls.pr_has_conflicts.return_value = None

    def plugin(self, settings: Settings) -> MagicMock:
        plugin = MagicMock(spec=ProviderPlugin)
        plugin.instance_id = "github"
        plugin.capabilities = GitHubProviderPlugin.capabilities
        plugin.client.return_value = self.forge
        plugin.pulls.return_value = self.pulls
        plugin.transport.return_value = GitHubTransport(
            self.auth, settings.providers["github"].host
        )
        self.last_plugin = plugin
        return plugin

    def comments(self) -> list[str]:
        """Bodies posted on the issue, in order."""
        return [c.args[1] for c in self.forge.post_comment.call_args_list]

    def pr_section(self, heading: str) -> str:
        """One ``## heading`` section of the PR body the run created."""
        body = self.pulls.create_pr.call_args.kwargs["body"]
        section = body.split(f"## {heading}\n", 1)[1]
        return re.split(r"\n\n(?:## |---\n)", section, maxsplit=1)[0]


def _make_task(**overrides: dict) -> Task:
    # target_repo/base_branch/workflow mirror the values
    # _make_settings() puts into repo_routes[0], now sourced from the Task
    # itself instead of Settings.
    defaults: dict = dict(
        delivery_id="delivery-1",
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

    shutil.rmtree("/tmp/test-executor-data", ignore_errors=True)


@pytest.fixture
def store() -> SQLiteStore:
    s = SQLiteStore(":memory:")
    s.init()
    return s


@pytest.fixture
def settings() -> Settings:
    return _make_settings()


# ── Helpers ────────────────────────────────────────────────────────


def _task_status(store: SQLiteStore, task_id: str) -> TaskStatus | None:
    t = store.get_task(task_id)
    return t.status if t else None


def _task_events(store: SQLiteStore, task_id: str) -> list[dict]:
    return [
        {"type": e.event_type, "payload": json.loads(e.payload_json)}
        for e in store.get_task_events(task_id)
    ]


# ── _extract_issue_title ───────────────────────────────────────────


class TestExtractIssueTitle:
    def test_from_valid_trigger(self) -> None:
        task = _make_task()
        assert _extract_issue_title(task) == "Fix broken link in docs"

    @pytest.mark.parametrize("trigger_json", [None, "", "not-json", "{}"])
    def test_missing_or_invalid_trigger(self, trigger_json) -> None:
        task = _make_task(trigger_json=trigger_json)
        assert _extract_issue_title(task) == "(unknown)"

    def test_missing_title(self) -> None:
        trigger = make_trigger_event(issue=make_issue(title=""))
        task = _make_task(trigger_json=trigger.model_dump_json())
        assert _extract_issue_title(task) == "(unknown)"


# ── _extract_author_login ──────────────────────────────────────────


class TestExtractAuthorLogin:
    def test_from_valid_trigger(self) -> None:
        task = _make_task()
        assert _extract_author_login(task) == "testuser"

    def test_missing_actor(self) -> None:
        trigger = make_trigger_event(actor=make_author(login=""))
        task = _make_task(trigger_json=trigger.model_dump_json())
        assert _extract_author_login(task) == "unknown"

    def test_invalid_trigger(self) -> None:
        task = _make_task(trigger_json="not-json")
        assert _extract_author_login(task) == "unknown"


# ── _parse_git_status ──────────────────────────────────────────────


class TestParseGitStatus:
    def test_modified_file(self) -> None:
        result = _parse_git_status(" M src/foo.py")
        assert result == [("src/foo.py", "modified")]

    def test_new_file(self) -> None:
        result = _parse_git_status("?? newfile.ts")
        assert result == [("newfile.ts", "added")]

    def test_multiple_files(self) -> None:
        output = " M src/foo.py\n?? newfile.ts\n D src/old.py"
        result = _parse_git_status(output)
        assert ("src/foo.py", "modified") in result
        assert ("newfile.ts", "added") in result
        assert ("src/old.py", "deleted") in result

    def test_renamed_file(self) -> None:
        result = _parse_git_status("R  old.py -> new.py")
        assert result == [("new.py", "renamed")]

    def test_empty_output(self) -> None:
        assert _parse_git_status("") == []

    def test_whitespace_only(self) -> None:
        assert _parse_git_status("  \n\n  ") == []


# ── _resolve_test_command (A10 / R5.6.5) ────────────────────────────


def _route_project(settings: Settings, issue_repo: str = "stateway-io/stateway-issues"):
    route = settings.route_for("github", issue_repo)
    assert route is not None
    return route.project


class TestResolveTestCommand:
    def test_config_wins_over_sentinel(self) -> None:
        settings = _make_settings(TEST_COMMAND="pytest -x")

        assert _resolve_test_command(_route_project(settings), "echo from-sentinel") == "pytest -x"

    def test_sentinel_used_when_config_empty(self) -> None:
        settings = _make_settings()

        assert _resolve_test_command(_route_project(settings), "npm test") == "npm test"

    def test_neither_config_nor_sentinel_returns_none(self) -> None:
        settings = _make_settings()

        assert _resolve_test_command(_route_project(settings), None) is None


# ── _consume_test_command_sentinel (Finding 1) ──────────────────────


class TestConsumeTestCommandSentinel:
    def test_reads_first_line_and_deletes_file(self, tmp_path: Path) -> None:
        sentinel = tmp_path / TEST_COMMAND_SENTINEL_FILENAME
        sentinel.write_text("npm test\n")

        result = _consume_test_command_sentinel(tmp_path)

        assert result == "npm test"
        assert not sentinel.exists()

    def test_uses_first_line_only(self, tmp_path: Path) -> None:
        sentinel = tmp_path / TEST_COMMAND_SENTINEL_FILENAME
        sentinel.write_text("pytest\nsome unrelated second line\n")

        result = _consume_test_command_sentinel(tmp_path)

        assert result == "pytest"
        assert not sentinel.exists()

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _consume_test_command_sentinel(tmp_path) is None

    def test_empty_sentinel_file_returns_none_and_deletes(self, tmp_path: Path) -> None:
        sentinel = tmp_path / TEST_COMMAND_SENTINEL_FILENAME
        sentinel.write_text("   \n")

        result = _consume_test_command_sentinel(tmp_path)

        assert result is None
        assert not sentinel.exists()


# ── _consume_checks_sentinel (issue #102) ──────────────────────────


class TestConsumeChecksSentinel:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert _consume_checks_sentinel(tmp_path) == []

    def test_parses_statuses_and_reasons_and_deletes_file(self, tmp_path: Path) -> None:
        sentinel = tmp_path / CHECKS_SENTINEL_FILENAME
        sentinel.write_text(
            "passed: npx tsc --noEmit\n"
            "not_run: npm test -- --forceExit — needs Postgres and Redis\n"
            "failed: npm run lint – 2 errors in src/main.ts\n"
        )

        checks = _consume_checks_sentinel(tmp_path)

        assert checks == [
            ReportedCheck("passed", "npx tsc --noEmit"),
            ReportedCheck("not_run", "npm test -- --forceExit", "needs Postgres and Redis"),
            ReportedCheck("failed", "npm run lint", "2 errors in src/main.ts"),
        ]
        assert not sentinel.exists()

    def test_tolerates_bullets_case_and_spelled_out_status(self, tmp_path: Path) -> None:
        (tmp_path / CHECKS_SENTINEL_FILENAME).write_text(
            "- Passed: pytest\n* not run: jest — timed out\nnot-run: tsc\n"
        )

        assert _consume_checks_sentinel(tmp_path) == [
            ReportedCheck("passed", "pytest"),
            ReportedCheck("not_run", "jest", "timed out"),
            ReportedCheck("not_run", "tsc"),
        ]

    def test_ascii_double_dash_stays_part_of_the_command(self, tmp_path: Path) -> None:
        (tmp_path / CHECKS_SENTINEL_FILENAME).write_text("passed: npm test -- --forceExit\n")

        assert _consume_checks_sentinel(tmp_path) == [
            ReportedCheck("passed", "npm test -- --forceExit")
        ]

    def test_drops_unknown_statuses_and_free_text(self, tmp_path: Path) -> None:
        (tmp_path / CHECKS_SENTINEL_FILENAME).write_text(
            "skipped: pytest\nI ran some things\n\npassed: ruff check\n"
        )

        assert _consume_checks_sentinel(tmp_path) == [ReportedCheck("passed", "ruff check")]

    def test_strips_backticks_and_caps_lengths_and_count(self, tmp_path: Path) -> None:
        lines = ["passed: `npm` test"]
        lines.append("not_run: " + "x" * 500 + " — " + "y" * 500)
        lines += [f"passed: check-{i}" for i in range(20)]
        (tmp_path / CHECKS_SENTINEL_FILENAME).write_text("\n".join(lines))

        checks = _consume_checks_sentinel(tmp_path)

        assert checks[0] == ReportedCheck("passed", "npm test")
        assert len(checks[1].command) <= 200
        assert len(checks[1].reason) <= 300
        assert len(checks) == 10

    def test_whitespace_only_file_returns_empty_and_deletes(self, tmp_path: Path) -> None:
        sentinel = tmp_path / CHECKS_SENTINEL_FILENAME
        sentinel.write_text("  \n\n")

        assert _consume_checks_sentinel(tmp_path) == []
        assert not sentinel.exists()


# ── _run_test_command ────────────────────────────────────────────────


class TestRunTestCommand:
    def test_passing_command_reports_passed(self, tmp_path: Path) -> None:
        passed, output = _run_test_command(tmp_path, "echo ok-output", timeout_seconds=5)

        assert passed is True
        assert "ok-output" in output

    def test_failing_command_reports_failed(self, tmp_path: Path) -> None:
        passed, output = _run_test_command(
            tmp_path, "echo boom-output && exit 1", timeout_seconds=5
        )

        assert passed is False
        assert "boom-output" in output

    def test_timeout_is_reported_as_failed(self, tmp_path: Path) -> None:
        passed, output = _run_test_command(tmp_path, "sleep 5", timeout_seconds=1)

        assert passed is False
        assert "timed out" in output

    def test_runs_in_the_given_workspace(self, tmp_path: Path) -> None:
        (tmp_path / "marker.txt").write_text("hello")

        passed, output = _run_test_command(tmp_path, "cat marker.txt", timeout_seconds=5)

        assert passed is True
        assert "hello" in output


# ── _prepare_toolchain ─────────────────────────────────────────────────


class TestPrepareToolchain:
    def test_disabled_globally_returns_empty_env(self, tmp_path: Path) -> None:
        settings = _make_settings(TOOLCHAINS_ENABLED=False)
        route = settings.route_for("github", "stateway-io/stateway-issues")
        task_log = MagicMock()

        with patch("issue_agent.runner.executor.ToolchainManager") as mock_manager_cls:
            result = _prepare_toolchain(tmp_path, settings, route, task_log)

        assert result == {}
        mock_manager_cls.assert_not_called()

    def test_project_toolchain_none_short_circuits_before_detect(self, tmp_path: Path) -> None:
        """A repo that opts out via project.toolchain: none must never pay
        for detection (Node.js version resolution hits nodejs.org over the
        network) — the override has to be checked before a ToolchainManager
        is even constructed, not after ``detect()`` already ran."""
        settings = _make_settings(
            REPO_ROUTES=[
                {
                    "issues": "stateway-io/stateway-issues",
                    "target": "stateway-io/stateway-agent-fixtures",
                    "project": {"toolchain": "none"},
                }
            ]
        )
        route = settings.route_for("github", "stateway-io/stateway-issues")
        task_log = MagicMock()

        with patch("issue_agent.runner.executor.ToolchainManager") as mock_manager_cls:
            result = _prepare_toolchain(tmp_path, settings, route, task_log)

        assert result == {}
        mock_manager_cls.assert_not_called()

    def test_auto_detects_ensures_and_installs_dependencies(self, tmp_path: Path) -> None:
        settings = _make_settings(TOOLCHAINS_NODEJS_MAX_OLD_SPACE_PERCENT="0")
        route = settings.route_for("github", "stateway-io/stateway-issues")
        task_log = MagicMock()
        spec = MagicMock()
        spec.name = "nodejs"

        with (
            patch("issue_agent.runner.executor.ToolchainManager") as mock_manager_cls,
            patch("issue_agent.runner.executor.install_project_dependencies") as mock_deps,
        ):
            manager = mock_manager_cls.return_value
            manager.detect.return_value = [spec]
            manager.ensure.return_value = {"PATH": "/data/tools/nodejs/20.17.0/bin"}
            result = _prepare_toolchain(tmp_path, settings, route, task_log)

        manager.detect.assert_called_once_with(tmp_path)
        manager.ensure.assert_called_once_with([spec])
        mock_deps.assert_called_once()
        # The Node heap cap is off for this repo, so the env passes through as is.
        assert result == {"PATH": "/data/tools/nodejs/20.17.0/bin"}

    def test_forced_toolchain_filters_detected_specs(self, tmp_path: Path) -> None:
        settings = _make_settings(
            REPO_ROUTES=[
                {
                    "issues": "stateway-io/stateway-issues",
                    "target": "stateway-io/stateway-agent-fixtures",
                    "project": {"toolchain": "nodejs"},
                }
            ]
        )
        route = settings.route_for("github", "stateway-io/stateway-issues")
        task_log = MagicMock()
        nodejs_spec = MagicMock()
        nodejs_spec.name = "nodejs"
        other_spec = MagicMock()
        other_spec.name = "python"

        with (
            patch("issue_agent.runner.executor.ToolchainManager") as mock_manager_cls,
            patch("issue_agent.runner.executor.install_project_dependencies"),
        ):
            manager = mock_manager_cls.return_value
            manager.detect.return_value = [nodejs_spec, other_spec]
            manager.ensure.return_value = {}
            _prepare_toolchain(tmp_path, settings, route, task_log)

        manager.ensure.assert_called_once_with([nodejs_spec])

    def _node_run(self, tmp_path: Path, **overrides):
        """Run ``_prepare_toolchain`` for a Node project with a stubbed manager."""
        settings = _make_settings(TOOLCHAINS_TOOLS_ROOT=str(tmp_path / "tools"), **overrides)
        route = settings.route_for("github", "stateway-io/stateway-issues")
        task_log = MagicMock()
        spec = MagicMock()
        spec.name = "nodejs"
        with (
            patch("issue_agent.runner.executor.ToolchainManager") as mock_manager_cls,
            patch("issue_agent.runner.executor.install_project_dependencies") as mock_deps,
            patch(
                "issue_agent.runner.executor.resolve_node_heap_mb", return_value=384
            ) as mock_resolve,
        ):
            manager = mock_manager_cls.return_value
            manager.detect.return_value = [spec]
            manager.ensure.return_value = {"PATH": "/data/tools/nodejs/20.17.0/bin"}
            result = _prepare_toolchain(tmp_path, settings, route, task_log)
        return settings, result, mock_deps, mock_resolve, task_log

    def test_node_env_gets_the_capped_node_shim_first_on_path(self, tmp_path: Path) -> None:
        _, result, _, _, _ = self._node_run(tmp_path)

        shim_dir = str(tmp_path / "tools" / "shims")
        assert result["PATH"] == f"{shim_dir}:/data/tools/nodejs/20.17.0/bin"
        assert result["ISSUE_AGENT_NODE_MAX_OLD_SPACE_MB"] == "384"
        assert (tmp_path / "tools" / "shims" / "node").is_file()

    def test_dependency_install_runs_under_the_same_cap(self, tmp_path: Path) -> None:
        """``npm ci`` is one of the heavy Node processes on the small VM."""
        _, result, mock_deps, _, _ = self._node_run(tmp_path)

        assert mock_deps.call_args.args[2] == result

    def test_cap_is_resolved_from_the_configured_percentage_and_override(
        self, tmp_path: Path
    ) -> None:
        _, _, _, mock_resolve, _ = self._node_run(
            tmp_path,
            TOOLCHAINS_NODEJS_MAX_OLD_SPACE_PERCENT="35",
            TOOLCHAINS_NODEJS_MAX_OLD_SPACE_MB="600",
        )

        mock_resolve.assert_called_once_with(35, 600)

    def test_the_resolved_cap_is_logged(self, tmp_path: Path) -> None:
        *_, task_log = self._node_run(tmp_path)

        events = [c.args[0] for c in task_log.info.call_args_list]
        assert "node_heap_cap_resolved" in events

    def test_a_disabled_cap_leaves_the_env_alone(self, tmp_path: Path) -> None:
        settings = _make_settings(TOOLCHAINS_TOOLS_ROOT=str(tmp_path / "tools"))
        route = settings.route_for("github", "stateway-io/stateway-issues")
        spec = MagicMock()
        spec.name = "nodejs"
        with (
            patch("issue_agent.runner.executor.ToolchainManager") as mock_manager_cls,
            patch("issue_agent.runner.executor.install_project_dependencies"),
            patch("issue_agent.runner.executor.resolve_node_heap_mb", return_value=None),
        ):
            manager = mock_manager_cls.return_value
            manager.detect.return_value = [spec]
            manager.ensure.return_value = {"PATH": "/p"}
            result = _prepare_toolchain(tmp_path, settings, route, MagicMock())

        assert result == {"PATH": "/p"}
        assert not (tmp_path / "tools" / "shims").exists()

    def test_a_shim_that_cannot_be_written_never_breaks_the_task(self, tmp_path: Path) -> None:
        settings = _make_settings(TOOLCHAINS_TOOLS_ROOT=str(tmp_path / "tools"))
        route = settings.route_for("github", "stateway-io/stateway-issues")
        task_log = MagicMock()
        spec = MagicMock()
        spec.name = "nodejs"
        with (
            patch("issue_agent.runner.executor.ToolchainManager") as mock_manager_cls,
            patch("issue_agent.runner.executor.install_project_dependencies"),
            patch("issue_agent.runner.executor.resolve_node_heap_mb", return_value=384),
            patch(
                "issue_agent.runner.executor.node_guard_env", side_effect=OSError("read-only fs")
            ),
        ):
            manager = mock_manager_cls.return_value
            manager.detect.return_value = [spec]
            manager.ensure.return_value = {"PATH": "/p"}
            result = _prepare_toolchain(tmp_path, settings, route, task_log)

        assert result == {"PATH": "/p"}
        assert "node_heap_cap_unavailable" in [c.args[0] for c in task_log.warning.call_args_list]

    def test_no_shim_for_a_repo_without_a_node_toolchain(self, tmp_path: Path) -> None:
        settings = _make_settings(TOOLCHAINS_TOOLS_ROOT=str(tmp_path / "tools"))
        route = settings.route_for("github", "stateway-io/stateway-issues")
        other = MagicMock()
        other.name = "python"
        with (
            patch("issue_agent.runner.executor.ToolchainManager") as mock_manager_cls,
            patch("issue_agent.runner.executor.install_project_dependencies"),
            patch("issue_agent.runner.executor.resolve_node_heap_mb", return_value=384),
        ):
            manager = mock_manager_cls.return_value
            manager.detect.return_value = [other]
            manager.ensure.return_value = {"PATH": "/p"}
            result = _prepare_toolchain(tmp_path, settings, route, MagicMock())

        assert result == {"PATH": "/p"}
        assert not (tmp_path / "tools" / "shims").exists()

    def test_ensure_failure_is_best_effort(self, tmp_path: Path) -> None:
        settings = _make_settings()
        route = settings.route_for("github", "stateway-io/stateway-issues")
        task_log = MagicMock()
        spec = MagicMock()
        spec.name = "nodejs"

        with patch("issue_agent.runner.executor.ToolchainManager") as mock_manager_cls:
            manager = mock_manager_cls.return_value
            manager.detect.return_value = [spec]
            manager.ensure.side_effect = RuntimeError("boom")
            result = _prepare_toolchain(tmp_path, settings, route, task_log)

        assert result == {}


# ── _build_testing_notes ──────────────────────────────────────────────


class TestBuildTestingNotes:
    def test_no_command_is_honest_and_never_claims_success(self) -> None:
        notes = _build_testing_notes(None, None)

        assert "[x]" not in notes
        assert "not" in notes.lower()

    def test_passed(self) -> None:
        assert _build_testing_notes("pytest", True) == "- [x] `pytest` passed"

    def test_failed(self) -> None:
        assert _build_testing_notes("pytest", False) == "- [ ] `pytest` failed (see log)"

    def test_partial_verification_without_harness_command(self) -> None:
        """Regression: tsc passed, the integration suite could not run, and
        no `.agent-test-command` was written. The footer must not claim that
        nothing was tested."""
        notes = _build_testing_notes(
            None,
            None,
            [
                ReportedCheck("passed", "npx tsc --noEmit"),
                ReportedCheck("not_run", "npm test", "needs Postgres and Redis"),
            ],
        )

        assert notes == (
            "- [ ] The harness did not re-run any test command.\n\n"
            "Reported by the agent (not re-run by the harness):\n"
            "- `npx tsc --noEmit` — passed\n"
            "- `npm test` — not run: needs Postgres and Redis"
        )
        assert "No test command was configured" not in notes

    def test_harness_result_comes_first_and_agent_report_is_never_checked(self) -> None:
        notes = _build_testing_notes(
            "pytest", True, [ReportedCheck("failed", "ruff check", "3 errors")]
        )

        harness, agent = notes.split("\n\n", 1)
        assert harness == "- [x] `pytest` passed"
        assert "[x]" not in agent
        assert "- `ruff check` — failed: 3 errors" in agent


# ── _resolve_pr_description ──────────────────────────────────────────


class TestResolvePRDescription:
    def test_uses_submission_when_present(self) -> None:
        assert (
            _resolve_pr_description("Fixed the null check in parser.py", "Fix broken link")
            == "Fixed the null check in parser.py"
        )

    def test_falls_back_to_issue_title_when_none(self) -> None:
        assert _resolve_pr_description(None, "Fix broken link") == "Fix broken link"

    def test_falls_back_to_issue_title_when_empty_string(self) -> None:
        assert _resolve_pr_description("", "Fix broken link") == "Fix broken link"

    def test_falls_back_to_issue_title_when_whitespace_only(self) -> None:
        assert _resolve_pr_description("   \n  ", "Fix broken link") == "Fix broken link"

    def test_strips_surrounding_whitespace(self) -> None:
        assert (
            _resolve_pr_description("  Fixed the bug.  \n", "Fix broken link") == "Fixed the bug."
        )

    def test_normal_length_submission_passes_through_unchanged(self) -> None:
        submission = "Fixed the null check in parser.py. " * 20  # well under the cap
        assert _resolve_pr_description(submission, "Fix broken link") == submission.strip()

    def test_oversized_submission_is_truncated_with_marker(self) -> None:
        oversized = "x" * 10_000

        result = _resolve_pr_description(oversized, "Fix broken link")

        assert len(result) <= 6000
        assert result.endswith("(truncated)")
        assert result.startswith("x" * 100)


# ── _build_fix_comment ─────────────────────────────────────────────


class TestFollowUpComments:
    """Regression: a follow-up that pushed onto an open PR was
    announced as "opened a PR with a proposed fix", with nothing on the PR."""

    def _work(self, task, **kw) -> IssueWork:
        return IssueWork(
            issue_repo=task.repo,
            issue_number=task.issue_number,
            target_repo=task.target_repo,
            base_branch="dev",
            work_branch="dev-x",
            **kw,
        )

    def test_fix_comment_says_updated_and_warns_on_conflicts(self) -> None:
        task = _make_task()
        result = AgentResult(success=True, cost_usd=0.05, iterations=42)
        comment = _build_fix_comment(
            task,
            _pr_ref(288, "o/r", "https://github.com"),
            result,
            "o/r",
            "Agent X",
            updated_existing=True,
            still_conflicting=True,
        )
        assert "updated the PR" in comment
        assert "opened a PR" not in comment
        assert "still reports conflicts" in comment
        assert "o/r#288" in comment

    def test_fix_comment_links_the_pr_on_the_configured_host(self) -> None:
        task = _make_task()
        result = AgentResult(success=True, cost_usd=0.05, iterations=4)
        comment = _build_fix_comment(
            task,
            _pr_ref(288, "o/r", "https://ghe.example.com"),
            result,
            "o/r",
            "Agent X",
        )
        assert "[o/r#288](https://ghe.example.com/o/r/pull/288)" in comment
        assert "https://github.com" not in comment

    def test_initial_comment_names_the_pr_being_updated(self) -> None:
        task = _make_task()
        comment = _build_initial_comment(task, self._work(task), "Agent X", pr_number=288)
        assert "to update PR #288" in comment
        assert "started working" not in comment

    def test_pr_update_comment_reports_merge_and_changes(self) -> None:
        task = _make_task()
        comment = _build_pr_update_comment(
            task,
            AgentResult(success=True, cost_usd=0.05, iterations=42),
            "Resolved the activeContext conflict.",
            [(".memory-bank/activeContext.md", "modified")],
            "- [x] `npm test` passed",
            "Agent X",
            BaseUpdate("conflicted", [(".memory-bank/activeContext.md", "both modified")]),
            still_conflicting=False,
        )
        assert f"{task.repo}#{task.issue_number}" in comment
        assert "conflicts resolved in `.memory-bank/activeContext.md`" in comment
        assert "Resolved the activeContext conflict." in comment
        assert "- `.memory-bank/activeContext.md` — modified" in comment
        assert "`npm test` passed" in comment
        assert task.id in comment
        assert "still reports conflicts" not in comment


class TestBuildFixComment:
    def test_contains_pr_link(self) -> None:
        task = _make_task()
        result = AgentResult(success=True, cost_usd=1.23, iterations=15)
        comment = _build_fix_comment(
            task,
            _pr_ref(101, "stateway-io/stateway", "https://github.com"),
            result,
            "stateway-io/stateway",
            "Agent X",
        )
        assert "stateway-io/stateway#101" in comment
        assert "stateway-io/stateway/pull/101" in comment
        assert "US$ 1.23" in comment
        assert "Iterations: 15" in comment
        assert task.id in comment

    def test_zero_cost(self) -> None:
        task = _make_task()
        result = AgentResult(success=True, cost_usd=0.0, iterations=0)
        comment = _build_fix_comment(
            task, _pr_ref(1, "r/o", "https://github.com"), result, "r/o", "Agent X"
        )
        assert "US$ 0.00" in comment

    def test_uses_configured_agent_name(self) -> None:
        task = _make_task()
        result = AgentResult(success=True, cost_usd=0.0, iterations=0)
        comment = _build_fix_comment(
            task, _pr_ref(1, "r/o", "https://github.com"), result, "r/o", "My Custom Bot"
        )
        assert "My Custom Bot" in comment
        assert "Stateway" not in comment

    def test_first_attempt_has_no_accumulated_total_line(self) -> None:
        """attempts == 0 (no resume) — the per-attempt cost already is the
        issue's total, so the extra line would be pure noise."""
        task = _make_task()
        result = AgentResult(success=True, cost_usd=0.01, iterations=16)
        work = IssueWork(
            issue_repo=task.repo,
            issue_number=task.issue_number,
            target_repo=task.target_repo,
            base_branch="main",
            work_branch="b",
            attempts=0,
            total_cost_usd=0.0,
            total_iterations=0,
        )
        comment = _build_fix_comment(
            task, _pr_ref(1, "r/o", "https://github.com"), result, "r/o", "Agent X", work
        )
        assert "Total accumulated" not in comment

    def test_resumed_attempt_shows_accumulated_total(self) -> None:
        """Regression: task
        bcf38bb3f06e4211ad145d50a0ca8833 (attempt 2) opened its PR showing
        "Cost: US$ 0.00" because that number is only this attempt's cost —
        attempt 1 had already spent US$ 0.02. The comment must also show
        the issue-wide total across every attempt."""
        task = _make_task()
        result = AgentResult(success=True, cost_usd=0.004, iterations=16)
        work = IssueWork(
            issue_repo=task.repo,
            issue_number=task.issue_number,
            target_repo=task.target_repo,
            base_branch="main",
            work_branch="b",
            attempts=1,
            total_cost_usd=0.02,
            total_iterations=40,
        )
        comment = _build_fix_comment(
            task, _pr_ref(1, "r/o", "https://github.com"), result, "r/o", "Agent X", work
        )
        assert "Cost: US$ 0.00" in comment  # this attempt alone, unchanged
        assert "Total accumulated for this issue: US$ 0.02" in comment
        assert "56 iterations across 2 attempts" in comment

    def test_carried_over_headline_names_the_branch_not_a_fresh_fix(self) -> None:
        """issue #89: the run wrote nothing — the fix was already committed
        by an earlier run that never opened a PR. "opened a PR with a
        proposed fix" would hide that, and the agent's own report below
        reads like "nothing to do here"."""
        task = _make_task()
        result = AgentResult(success=True, cost_usd=0.10, iterations=3)
        comment = _build_fix_comment(
            task,
            _pr_ref(271, "r/o", "https://github.com"),
            result,
            "r/o",
            "Agent X",
            carried_over_commits=1,
            branch="agent/issue-42-fix",
        )
        assert "already committed" in comment
        assert "proposed fix" not in comment
        assert "`agent/issue-42-fix` was 1 commit ahead of `main`" in comment

    def test_carried_over_pluralises_the_commit_count(self) -> None:
        task = _make_task()
        result = AgentResult(success=True, cost_usd=0.10, iterations=3)
        comment = _build_fix_comment(
            task,
            _pr_ref(271, "r/o", "https://github.com"),
            result,
            "r/o",
            "Agent X",
            carried_over_commits=3,
            branch="b",
        )
        assert "3 commits ahead of `main`" in comment

    def test_without_carried_over_the_headline_is_unchanged(self) -> None:
        task = _make_task()
        result = AgentResult(success=True, cost_usd=0.10, iterations=3)
        comment = _build_fix_comment(
            task, _pr_ref(271, "r/o", "https://github.com"), result, "r/o", "Agent X"
        )
        assert "opened a PR with a proposed fix" in comment
        assert "already committed" not in comment


# ── _build_abort_comment ───────────────────────────────────────────


class TestBuildAbortComment:
    def test_budget_exceeded(self) -> None:
        task = _make_task()
        result = AgentResult(success=False, error="budget_exceeded", cost_usd=1.50, iterations=8)
        comment = _build_abort_comment(task, result, "Agent X")
        assert "budget" in comment.lower()
        assert "aborted" in comment
        assert "US$ 1.50" in comment
        assert "#42" in comment

    def test_no_changes(self) -> None:
        task = _make_task()
        result = AgentResult(success=False, error="no_changes", cost_usd=0.50, iterations=5)
        comment = _build_abort_comment(task, result, "Agent X")
        assert "no changes" in comment.lower()

    def test_no_changes_is_not_framed_as_a_failure(self) -> None:
        # The agent ran to completion and concluded nothing
        # needed changing — often because the fix had already landed. The
        # old "execution aborted. The agent produced no changes." read as an
        # error and buried the verdict.
        task = _make_task()
        result = AgentResult(success=False, error="no_changes", cost_usd=0.01, iterations=15)
        comment = _build_abort_comment(task, result, "Agent X")
        assert "aborted" not in comment.lower()
        assert "no code change was needed" in comment

    def test_no_changes_surfaces_the_agent_report(self) -> None:
        # The provenance the prompt asks for (commit SHA + closure
        # recommendation) lives in the agent's submission, so the comment is
        # worthless if it drops it.
        task = _make_task()
        result = AgentResult(
            success=False,
            error="no_changes",
            cost_usd=0.01,
            iterations=15,
            submission=(
                "Already fixed in 35c7c55 (PR #261). This issue can be closed with no change."
            ),
        )
        comment = _build_abort_comment(task, result, "Agent X")
        assert "35c7c55" in comment
        assert "closed with no change" in comment

    def test_other_errors_still_read_as_aborted(self) -> None:
        task = _make_task()
        result = AgentResult(success=False, error="task_timeout", cost_usd=1.0, iterations=5)
        comment = _build_abort_comment(task, result, "Agent X")
        assert "aborted" in comment.lower()

    def test_format_error(self) -> None:
        task = _make_task()
        result = AgentResult(success=False, error="format_error", cost_usd=0.20, iterations=4)
        comment = _build_abort_comment(task, result, "Agent X")
        assert "format" in comment.lower()
        assert "Erro: format_error" not in comment

    def test_unknown_error(self) -> None:
        task = _make_task()
        result = AgentResult(success=False, error="something_else", cost_usd=0.0, iterations=0)
        comment = _build_abort_comment(task, result, "Agent X")
        assert "something_else" in comment

    def test_task_timeout(self) -> None:
        task = _make_task()
        result = AgentResult(success=False, error="task_timeout", cost_usd=1.00, iterations=5)
        comment = _build_abort_comment(task, result, "Agent X")
        assert "maximum" in comment.lower()
        assert "execution time" in comment.lower()
        assert "US$ 1.00" in comment

    def test_uses_configured_agent_name(self) -> None:
        task = _make_task()
        result = AgentResult(success=False, error="no_changes", cost_usd=0.0, iterations=0)
        comment = _build_abort_comment(task, result, "My Custom Bot")
        assert "My Custom Bot" in comment
        assert "Stateway" not in comment

    def test_iteration_limit_exceeded_mentions_iterations_not_budget(self) -> None:
        """Regression for task
        6d1970e871f84e76ad1b75091d0df573: only the step limit fired (cost
        was $0.02, nowhere near the cost cap), so the comment must say
        "iterations", not blame it on the dollar budget."""
        task = _make_task()
        result = AgentResult(
            success=False, error="iteration_limit_exceeded", cost_usd=0.02, iterations=40
        )
        comment = _build_abort_comment(task, result, "Agent X")
        assert "iterations" in comment.lower()
        assert "US$ 0.02" in comment

    def test_cost_limit_exceeded_mentions_cost(self) -> None:
        task = _make_task()
        result = AgentResult(
            success=False, error="cost_limit_exceeded", cost_usd=2.00, iterations=5
        )
        comment = _build_abort_comment(task, result, "Agent X")
        assert "cost" in comment.lower()

    def test_resumed_attempt_shows_accumulated_total(self) -> None:
        task = _make_task()
        result = AgentResult(
            success=False, error="iteration_limit_exceeded", cost_usd=0.01, iterations=16
        )
        work = IssueWork(
            issue_repo=task.repo,
            issue_number=task.issue_number,
            target_repo=task.target_repo,
            base_branch="main",
            work_branch="b",
            attempts=1,
            total_cost_usd=0.02,
            total_iterations=40,
        )
        comment = _build_abort_comment(task, result, "Agent X", work)
        assert "Total accumulated for this issue: US$ 0.03" in comment
        assert "56 iterations across 2 attempts" in comment

    def test_provider_budget_exceeded_explains_how_to_resume(self) -> None:
        """The OpenRouter budget guardrail."""
        task = _make_task()
        result = AgentResult(
            success=False,
            error="provider_budget_exceeded",
            cost_usd=0.0,
            iterations=1,
            error_detail=(
                "Budget limit exceeded (monthly limit). $10.02 of $10.00 used; "
                "resets 2026-11-01T00:00:00.000Z."
            ),
            provider_error=ProviderError(
                message="Budget limit exceeded",
                code=403,
                resets_at="2026-11-01T00:00:00.000Z",
            ),
        )
        comment = _build_abort_comment(task, result, "Agent X")
        assert "spending limit was reached" in comment
        assert "without retrying" in comment
        assert "reset on 2026-11-01" in comment
        assert "comment on this issue to resume" in comment
        assert "> Provider message: Budget limit exceeded (monthly limit)." in comment

    def test_error_detail_is_quoted_on_its_own_line_not_in_parentheses(self) -> None:
        """The detail used to be spliced as ``… retries (detail).`` — with a
        detail cut mid-JSON, the closing paren read as unmatched (#352)."""
        task = _make_task()
        result = AgentResult(
            success=False,
            error="llm_error",
            cost_usd=0.0,
            iterations=1,
            error_detail="APIError: [SSL: UNEXPECTED_EOF]\nsecond line",
        )
        comment = _build_abort_comment(task, result, "Agent X")
        reason = comment.split("\n\n")[1]
        assert reason == "The LLM provider call kept failing after several retries."
        assert "\n> Provider error: APIError: [SSL: UNEXPECTED_EOF] second line\n" in comment
        assert comment.count("(") == comment.count(")")

    def test_agent_crashed_detail_is_labelled_error(self) -> None:
        task = _make_task()
        result = AgentResult(
            success=False,
            error="agent_crashed",
            error_detail="RuntimeError: boom",
        )
        comment = _build_abort_comment(task, result, "Agent X")
        assert "The agent crashed unexpectedly.\n\n> Error: RuntimeError: boom" in comment


# ── run_task ───────────────────────────────────────────────────────


class TestRunTask:
    def test_route_not_found_fails_recoverable_with_explicit_message(
        self, store: SQLiteStore, settings: Settings
    ) -> None:
        """R5.2: if task.repo no longer resolves via settings.route_for()
        (e.g. config.yaml's repos: list changed between webhook enqueue and
        worker execution), run_task must fail the task explicitly instead of
        letting an AttributeError/None-access bubble out further down (e.g.
        from task.target_repo, which is unrelated to this and would still
        exist — the missing piece is the route itself)."""
        unconfigured_repo = "stateway-io/not-in-repo-routes"
        task = _make_task(repo=unconfigured_repo)
        store.insert_task(task)
        fetched = store.fetch_next()

        assert settings.route_for("github", unconfigured_repo) is None  # sanity check

        run_task(store, fetched, settings, _GitHub().plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.FAILED_RECOVERABLE
        assert updated.error is not None
        assert "route" in updated.error.lower()
        assert unconfigured_repo in updated.error

        events = _task_events(store, task.id)
        route_events = [e for e in events if e["payload"].get("step") == "route_resolution"]
        assert len(route_events) == 1

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_uses_task_target_repo_and_base_branch_not_route_defaults(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        """The whole point of multi-repo routing: target_repo/
        base_branch must come from the Task, not from the resolved
        RepoRoute. settings.route_for(task.provider_instance, task.repo) here resolves to
        "stateway-io/stateway-agent-fixtures" / "main" (route defaults), but
        the Task carries different values — if the code regressed to reading
        route.target_repo/route.base_branch instead of task.target_repo/
        task.base_branch, this test would still clone/open the PR against
        the route's values instead of the task's, and fail below."""
        gh = _GitHub()
        route = settings.route_for("github", "stateway-io/stateway-issues")
        assert route is not None
        assert route.target_repo == "stateway-io/stateway-agent-fixtures"
        assert route.base_branch == "main"

        task = _make_task(
            target_repo="owner/task-override-repo",
            base_branch="task-override-branch",
        )
        assert task.target_repo != route.target_repo
        assert task.base_branch != route.base_branch
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.50, iterations=10)
        mock_resolve.return_value = runner

        mock_git.side_effect = [
            "",  # checkout -b
            " M src/foo.py\n",  # status
            "",  # add
            "",  # commit
            "",  # push
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.SUCCEEDED

        # Workspace must be built against the task's target repo, not the route's.
        ws_call_args = mock_ws.call_args[0]
        assert ws_call_args[1] == "owner/task-override-repo"

        # clone() must target the task's base branch, not the route's.
        mock_ws_instance.clone.assert_called_once_with(target_branch="task-override-branch")

        # PR creation must target the task's repo/base branch.
        gh.last_plugin.pulls.assert_called_once_with("owner/task-override-repo")
        assert gh.pulls.create_pr.call_args.kwargs["base"] == "task-override-branch"

        # The fix comment must also reference the task's target repo.
        comment_body = gh.comments()[-1]
        assert "owner/task-override-repo" in comment_body

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_gitflow_flow_branches_and_targets_base_branch(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
    ) -> None:
        """R5.3.1: a gitflow-routed pair must branch off `base_branch`
        itself (not the trunk_based `<branch_prefix>/issue-<n>-<slug>`
        shape) and open the PR against that same branch, honoring
        route.draft/route.labels too. If the code hardcoded/ignored
        route.workflow, route.branch_prefix, route.draft or route.labels —
        e.g. kept always naming the branch the trunk_based way, or
        never threaded draft/labels through — this test's assertions on the
        exact call arguments below would fail even though a trunk_based
        test would still pass."""
        gh = _GitHub()
        settings = _make_settings(
            REPO_ROUTES=[
                {
                    "issues": "stateway-io/stateway-issues",
                    "target": "stateway-io/stateway-agent-fixtures",
                    "workflow": "gitflow",
                    "base_branch": "dev",
                    "branch_prefix": "agent",
                    "draft": True,
                    "labels": ["needs-review"],
                }
            ]
        )
        task = _make_task(
            target_repo="stateway-io/stateway-agent-fixtures",
            base_branch="dev",
            workflow="gitflow",
        )
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.50, iterations=10)
        mock_resolve.return_value = runner

        mock_git.side_effect = [
            "",  # checkout -b
            " M src/foo.py\n",  # status
            "",  # add
            "",  # commit
            "",  # push
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.SUCCEEDED

        # task.workflow and task.base_branch (not the trunk_based defaults)
        # shape the branch: gitflow branches off the base itself.

        checkout_call = mock_git.call_args_list[0]
        assert checkout_call.args[1:4] == (
            "checkout",
            "-b",
            "dev-fix-broken-link-in-docs",
        )

        push_call = mock_git.call_args_list[-1]
        assert "dev-fix-broken-link-in-docs" in push_call.args

        gh.last_plugin.pulls.assert_called_once_with("stateway-io/stateway-agent-fixtures")
        pr_call = gh.pulls.create_pr.call_args
        assert pr_call.kwargs["head"] == "dev-fix-broken-link-in-docs"
        assert pr_call.kwargs["base"] == "dev"
        assert pr_call.kwargs["draft"] is True
        assert pr_call.kwargs["labels"] == ["needs-review"]

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_task_workflow_wins_over_stale_route_workflow(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
    ) -> None:
        """Task.workflow freezes the workflow
        decision at webhook-ingestion time (queue/models.py). By the time
        run_task() executes, config.yaml's repo_routes entry for this pair
        may have changed — e.g. an operator flipped it from trunk_based to
        gitflow, or vice-versa — so settings.route_for(task.provider_instance, task.repo).workflow
        reflects the *current* config, not what was true when the webhook
        arrived. The branch name must be driven by task.workflow, never
        route.workflow: here the route has since reverted to trunk_based
        while the task itself still carries workflow="gitflow" from when it
        was enqueued, and the branch must still come out gitflow-shaped."""
        gh = _GitHub()
        settings = _make_settings(
            REPO_ROUTES=[
                {
                    "issues": "stateway-io/stateway-issues",
                    "target": "stateway-io/stateway-agent-fixtures",
                    # Config as of *now* — trunk_based. The task below was
                    # enqueued back when this pair was still gitflow.
                    "workflow": "trunk_based",
                    "branch_prefix": "bot",
                }
            ]
        )
        route = settings.route_for("github", "stateway-io/stateway-issues")
        assert route is not None
        assert route.workflow == "trunk_based"  # sanity: config has since changed

        task = _make_task(
            target_repo="stateway-io/stateway-agent-fixtures",
            base_branch="dev",
            workflow="gitflow",  # frozen at webhook time, before the config changed
        )
        store.insert_task(task)
        fetched = store.fetch_next()

        # Oracle: what pr_text.branch_name() actually produces for
        # workflow="gitflow" — not a hand-picked literal — so this test
        # fails if the gitflow branch-naming logic itself ever changes
        # shape, not just if run_task() reads the wrong workflow.
        from issue_agent.core.pr_text import branch_name

        expected_branch = branch_name(
            task.issue_number,
            "Fix broken link in docs",
            "gitflow",
            route.branch_prefix,
            "dev",
            slug_max_length=settings.pr_slug_max_length,
        )
        assert expected_branch == "dev-fix-broken-link-in-docs"  # sanity
        assert not expected_branch.startswith("bot/issue-")  # not trunk_based-shaped

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.50, iterations=10)
        mock_resolve.return_value = runner

        mock_git.side_effect = [
            "",  # checkout -b
            " M src/foo.py\n",  # status
            "",  # add
            "",  # commit
            "",  # push
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        # task.workflow ("gitflow"), not route.workflow ("trunk_based"),
        # names the branch — this is the crux of the fix.

        checkout_call = mock_git.call_args_list[0]
        assert checkout_call.args[1:4] == ("checkout", "-b", expected_branch)

        pr_call = gh.pulls.create_pr.call_args
        assert pr_call.kwargs["head"] == expected_branch
        assert pr_call.kwargs["base"] == "dev"

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.SUCCEEDED

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_git_credential_is_the_target_repos(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        """The task carries no credential: the clone and the push
        authenticate through the plugin's transport, which resolves the
        target repo's own credential."""
        gh = _GitHub()
        task = _make_task(target_repo="owner/migrated-repo")
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.50, iterations=10)
        mock_resolve.return_value = runner

        mock_git.side_effect = [
            "",  # checkout -b
            " M src/foo.py\n",  # status
            "",  # add
            "",  # commit
            "",  # push
        ]

        plugin = gh.plugin(settings)
        run_task(store, fetched, settings, plugin)

        assert mock_ws.call_args[0][3] is plugin.transport.return_value
        gh.auth.token_for.assert_called_with("owner/migrated-repo")
        push = mock_git.call_args_list[-1]
        assert push.args[1:3] == ("push", "-f")
        assert push.kwargs["env"]["GIT_CONFIG_KEY_0"] == "http.https://github.com.extraHeader"

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.SUCCEEDED

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_success_flow(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.50, iterations=10)
        mock_resolve.return_value = runner

        mock_git.side_effect = [
            "",  # checkout -b
            " M src/foo.py\n?? new.ts\n",  # status
            "",  # add
            "",  # commit
            "",  # push
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.SUCCEEDED
        assert updated.pr_number == 101
        assert updated.cost_usd == 0.50
        assert updated.iterations == 10
        assert updated.completed_at is not None

        events = _task_events(store, task.id)
        succeeded_events = [e for e in events if e["payload"].get("to") == "succeeded"]
        assert len(succeeded_events) == 1

        mock_ws_instance.clone.assert_called_once()
        runner.run.assert_called_once()
        gh.pulls.create_pr.assert_called_once()
        # initial "started work" comment (R1) + the fix comment
        assert len(gh.comments()) == 2
        assert "PR" in gh.comments()[-1]
        mock_ws_instance.cleanup.assert_called_once()

        testing_notes = gh.pr_section("How I tested")
        assert "[x]" not in testing_notes
        assert "not" in testing_notes.lower()

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_success_flow_runs_configured_test_command_and_reports_pass(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        tmp_path: Path,
    ) -> None:
        gh = _GitHub()
        settings = _make_settings(TEST_COMMAND="true")
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = tmp_path
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.50, iterations=10)
        mock_resolve.return_value = runner

        mock_git.side_effect = ["", " M src/foo.py\n", "", "", ""]

        run_task(store, fetched, settings, gh.plugin(settings))

        testing_notes = gh.pr_section("How I tested")
        assert testing_notes == "- [x] `true` passed"

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_success_flow_runs_configured_test_command_and_reports_failure(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        tmp_path: Path,
    ) -> None:
        gh = _GitHub()
        # Isolated from the auto-retry-on-test-failure feature (covered
        # separately by TestTestFailureRetryLoop) — this test is only about
        # _build_testing_notes' formatting on a single, non-retried failure.
        settings = _make_settings(TEST_COMMAND="false", RESUME_RETRY_ON_TEST_FAILURE=False)
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = tmp_path
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.50, iterations=10)
        mock_resolve.return_value = runner

        mock_git.side_effect = ["", " M src/foo.py\n", "", "", ""]

        run_task(store, fetched, settings, gh.plugin(settings))

        testing_notes = gh.pr_section("How I tested")
        assert testing_notes == "- [ ] `false` failed (see log)"

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_success_flow_uses_sentinel_file_when_config_empty(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        gh = _GitHub()
        (tmp_path / TEST_COMMAND_SENTINEL_FILENAME).write_text("true\n")

        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = tmp_path
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.50, iterations=10)
        mock_resolve.return_value = runner

        mock_git.side_effect = ["", " M src/foo.py\n", "", "", ""]

        run_task(store, fetched, settings, gh.plugin(settings))

        testing_notes = gh.pr_section("How I tested")
        assert testing_notes == "- [x] `true` passed"

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_success_flow_config_wins_over_sentinel(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        tmp_path: Path,
    ) -> None:
        gh = _GitHub()
        settings = _make_settings(TEST_COMMAND="true")
        (tmp_path / TEST_COMMAND_SENTINEL_FILENAME).write_text("false\n")

        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = tmp_path
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.50, iterations=10)
        mock_resolve.return_value = runner

        mock_git.side_effect = ["", " M src/foo.py\n", "", "", ""]

        run_task(store, fetched, settings, gh.plugin(settings))

        testing_notes = gh.pr_section("How I tested")
        assert testing_notes == "- [x] `true` passed"

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_success_flow_uses_agent_submission_as_pr_description(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(
            success=True,
            cost_usd=0.50,
            iterations=10,
            submission="Replaced the broken relative link with the absolute one.",
        )
        mock_resolve.return_value = runner

        mock_git.side_effect = ["", " M src/foo.py\n", "", "", ""]

        run_task(store, fetched, settings, gh.plugin(settings))

        description = gh.pr_section("Summary")
        assert description == "Replaced the broken relative link with the absolute one."

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_success_flow_falls_back_to_issue_title_when_no_submission(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(
            success=True, cost_usd=0.50, iterations=10, submission=None
        )
        mock_resolve.return_value = runner

        mock_git.side_effect = ["", " M src/foo.py\n", "", "", ""]

        run_task(store, fetched, settings, gh.plugin(settings))

        description = gh.pr_section("Summary")
        assert description == "Fix broken link in docs"  # the task's issue title

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_daily_budget_accumulated_on_success(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.50, iterations=10)
        mock_resolve.return_value = runner

        mock_git.side_effect = [
            "",
            " M src/foo.py\n",
            "",
            "",
            "",
            "",
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        budget = store.get_daily_budget(today, task.target_repo)
        assert budget.total_cost_usd == 0.50
        assert budget.task_count == 1

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_clone_failure(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.side_effect = RuntimeError("clone failed")
        mock_ws.return_value = mock_ws_instance

        run_task(store, fetched, settings, gh.plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.FAILED_RECOVERABLE
        assert updated.error is not None

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_clone_failure_missing_base_branch_reports_default_branch_diagnostic(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        """R5.3.5: when `ws.clone(target_branch=task.base_branch)` fails
        (e.g. the configured base_branch doesn't exist in target_repo), the
        persisted error must name the offending base_branch, the target
        repo, and the repo's actual default branch — not just re-surface
        the raw, generic clone exception string. If the code fell back to
        `str(exc)` unconditionally instead of consulting
        the target repo's default branch first, this test's
        assertions on the diagnostic wording would fail even though the
        task would still (correctly) end up FAILED_RECOVERABLE."""
        gh = _GitHub()
        task = _make_task(base_branch="release-branch-that-does-not-exist")
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.side_effect = RuntimeError("reference is not a tree")
        mock_ws.return_value = mock_ws_instance
        gh.forge.get_default_branch.return_value = "main"

        plugin = gh.plugin(settings)
        run_task(store, fetched, settings, plugin)

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.FAILED_RECOVERABLE
        assert updated.error is not None
        assert "release-branch-that-does-not-exist" in updated.error
        assert task.target_repo in updated.error
        assert "main" in updated.error
        assert "reference is not a tree" not in updated.error

        plugin.client.assert_any_call(task.target_repo)
        gh.forge.get_default_branch.assert_called_once_with()

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_clone_failure_falls_back_to_raw_error_when_default_branch_lookup_fails(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        """If the default-branch lookup itself fails (e.g. the app has no
        access to target_repo at all), run_task must still fail the task
        with the original clone error instead of raising a second,
        unhandled exception out of the diagnostic path."""
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.side_effect = RuntimeError("clone failed")
        mock_ws.return_value = mock_ws_instance
        gh.forge.get_default_branch.side_effect = RuntimeError("no access")

        run_task(store, fetched, settings, gh.plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.FAILED_RECOVERABLE
        assert updated.error == "clone failed"

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_agent_failure(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = mock_ws_instance

        mock_git.return_value = ""

        runner = MagicMock()
        runner.run.return_value = AgentResult(
            success=False, error="agent_crashed", cost_usd=0.10, iterations=3
        )
        mock_resolve.return_value = runner

        run_task(store, fetched, settings, gh.plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.FAILED_TERMINAL
        assert "agent_crashed" in (updated.error or "")

        # initial "started work" comment (R1) + the abort comment
        assert len(gh.comments()) == 2
        assert "aborted" in gh.comments()[-1].lower()

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_budget_exceeded(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = mock_ws_instance

        mock_git.return_value = ""

        runner = MagicMock()
        runner.run.return_value = AgentResult(
            success=False, error="budget_exceeded", cost_usd=2.00, iterations=20
        )
        mock_resolve.return_value = runner

        run_task(store, fetched, settings, gh.plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.ABORTED_BUDGET
        assert updated.cost_usd == 2.00
        assert updated.iterations == 20

        assert len(gh.comments()) == 2  # initial (R1) + abort
        assert "aborted" in gh.comments()[-1].lower()

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_daily_budget_accumulated_on_abort(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = mock_ws_instance

        mock_git.return_value = ""

        runner = MagicMock()
        runner.run.return_value = AgentResult(
            success=False, error="budget_exceeded", cost_usd=2.00, iterations=20
        )
        mock_resolve.return_value = runner

        run_task(store, fetched, settings, gh.plugin(settings))

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        budget = store.get_daily_budget(today, task.target_repo)
        assert budget.total_cost_usd == 2.00
        assert budget.task_count == 1

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_task_timeout_maps_to_aborted_budget(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = mock_ws_instance

        mock_git.return_value = ""

        runner = MagicMock()
        runner.run.return_value = AgentResult(
            success=False, error="task_timeout", cost_usd=1.50, iterations=15
        )
        mock_resolve.return_value = runner

        run_task(store, fetched, settings, gh.plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.ABORTED_BUDGET
        assert updated.cost_usd == 1.50
        assert updated.iterations == 15

        assert len(gh.comments()) == 2  # initial (R1) + abort
        assert "aborted" in gh.comments()[-1].lower()

    @patch("issue_agent.runner.executor.send_budget_alert")
    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_cost_unavailable_aborts_with_alert_and_no_pr(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        mock_alert: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = mock_ws_instance
        mock_git.return_value = ""

        runner = MagicMock()
        runner.run.return_value = AgentResult(
            success=False, error="cost_unavailable", cost_usd=0.12, iterations=2
        )
        mock_resolve.return_value = runner

        run_task(store, fetched, settings, gh.plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.ABORTED_BUDGET
        assert updated.error == "cost_unavailable"
        mock_alert.assert_called_once()
        gh.pulls.create_pr.assert_not_called()

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_no_changes(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = mock_ws_instance

        mock_git.side_effect = [
            "",  # checkout -b
            "",  # status (empty)
            "0",  # rev-list --count <start_sha>..HEAD (agent made no commits)
        ]

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.30, iterations=5)
        mock_resolve.return_value = runner

        run_task(store, fetched, settings, gh.plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.ABORTED_POLICY
        assert "no_changes" in (updated.error or "")

        assert len(gh.comments()) == 2  # initial (R1) + abort
        # `no_changes` is a verdict, not a failure: the comment leads with
        # that framing so the agent's own report (which must cite the commit
        # that already landed the fix) reads as the content, not as an error.
        comment = gh.comments()[-1]
        assert "no changes" in comment.lower()
        assert "aborted" not in comment.lower()

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_agent_self_committed_is_salvaged_not_aborted(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        """The agent committed its own edits (e.g. following the target repo's
        CLAUDE.md) instead of leaving them staged. `git status --porcelain` is
        then clean — but the work is real. The executor must fold those
        commits back into the tree and open the PR, not abort as no_changes."""
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = mock_ws_instance

        mock_git.side_effect = [
            "",  # checkout -b
            "",  # status (empty — agent committed)
            "1",  # rev-list --count <start_sha>..HEAD
            "",  # checkout -B <branch>
            "",  # reset --soft <start_sha>
            " M docs/x.mdx\n",  # status (re-read after salvage)
            "",  # add -A
            "",  # commit
            "",  # push
        ]

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.02, iterations=19)
        mock_resolve.return_value = runner

        run_task(store, fetched, settings, gh.plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.SUCCEEDED
        gh.pulls.create_pr.assert_called_once()

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_push_failure(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.50, iterations=10)
        mock_resolve.return_value = runner

        mock_git.side_effect = [
            "",  # checkout -b
            " M src/foo.py\n",  # status
            "",  # add
            "",  # commit
            RuntimeError("push failed"),  # push
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.FAILED_RECOVERABLE

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_pr_creation_failure(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.50, iterations=10)
        mock_resolve.return_value = runner

        mock_git.side_effect = [
            "",  # checkout -b
            " M src/foo.py\n",  # status
            "",  # add
            "",  # commit
            "",  # push
        ]
        gh.pulls.create_pr.side_effect = RuntimeError("pr creation failed")

        run_task(store, fetched, settings, gh.plugin(settings))

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.FAILED_RECOVERABLE

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_writes_audit_entry(
        self,
        mock_resolve: MagicMock,
        mock_ws: MagicMock,
        mock_git: MagicMock,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.50, iterations=10)
        mock_resolve.return_value = runner

        mock_git.side_effect = [
            "",  # checkout -b
            " M src/foo.py\n",  # status
            "",  # add
            "",  # commit
            "",  # push
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        import time

        today = time.strftime("%Y-%m-%d", time.gmtime())
        audit_path = Path(settings.data_dir) / f"audit.{today}.log"
        assert audit_path.exists()
        lines = audit_path.read_text().strip().split("\n")
        assert any("task_processed" in line for line in lines)
        assert any('"outcome": "succeeded"' in line for line in lines)


class TestTestFailureRetryLoop:
    """Bounded, automatic retry when the harness's own post-push test
    verification fails — reuses resume_max_attempts (no parallel counter)
    and re-checks the daily budget before each retry, never a new terminal
    outcome: exhaustion always falls through to the same PR-creation path as
    today, just with a failing testing_notes line."""

    @patch("issue_agent.runner.executor._run_test_command")
    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_retry_fires_and_eventually_succeeds(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        mock_run_test,
        store: SQLiteStore,
        tmp_path: Path,
    ) -> None:
        gh = _GitHub()
        settings = _make_settings(TEST_COMMAND="npm test")
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = tmp_path
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.side_effect = [
            AgentResult(success=True, cost_usd=0.50, iterations=10),
            AgentResult(success=True, cost_usd=0.10, iterations=3),
        ]
        mock_resolve.return_value = runner

        mock_run_test.side_effect = [(False, "1 failing"), (True, "all passing")]

        mock_git.side_effect = [
            "",  # checkout -b
            " M src/foo.py\n",  # status (iteration 1)
            "",  # add
            "",  # commit
            "",  # push
            " M src/foo.py\n",  # status (iteration 2, retry)
            "",  # add
            "",  # commit
            "",  # push
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        assert runner.run.call_count == 2
        retry_comments = [c for c in gh.comments() if "retrying automatically" in c]
        assert len(retry_comments) == 1
        assert "attempt 1/3" in retry_comments[0]

        testing_notes = gh.pr_section("How I tested")
        assert testing_notes == "- [x] `npm test` passed"

        work = store.get_issue_work("github", "stateway-io/stateway-issues", 42)
        assert work.state == "closed"
        assert work.closed_reason == "pr_opened"
        assert work.total_cost_usd == pytest.approx(0.60)
        assert work.total_iterations == 13
        assert work.attempts == 1

    @patch("issue_agent.runner.executor._run_test_command")
    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_retry_exhausts_attempts_falls_through_to_pr(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        mock_run_test,
        store: SQLiteStore,
        tmp_path: Path,
    ) -> None:
        gh = _GitHub()
        settings = _make_settings(TEST_COMMAND="npm test", RESUME_MAX_ATTEMPTS=2)
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = tmp_path
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.10, iterations=5)
        mock_resolve.return_value = runner

        mock_run_test.return_value = (False, "still failing")

        # RESUME_MAX_ATTEMPTS=2 means _decide_test_retry allows exactly one
        # retry (attempts 0 -> 1 < 2, then 1 -> 2 is NOT < 2) — two loop
        # passes total, each a full checkout.../push cycle.
        mock_git.side_effect = [
            "",  # checkout -b
            " M src/foo.py\n",  # status (iteration 1)
            "",  # add
            "",  # commit
            "",  # push
            " M src/foo.py\n",  # status (iteration 2, retry)
            "",  # add
            "",  # commit
            "",  # push
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        assert runner.run.call_count == 2
        testing_notes = gh.pr_section("How I tested")
        assert testing_notes == "- [ ] `npm test` failed (see log)"

        work = store.get_issue_work("github", "stateway-io/stateway-issues", 42)
        assert work.state == "closed"
        assert work.closed_reason == "pr_opened"  # NOT max_attempts — a human still gets a PR

    @patch("issue_agent.runner.executor.check_daily_budget")
    @patch("issue_agent.runner.executor._run_test_command")
    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_retry_stops_when_daily_budget_exhausted(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        mock_run_test,
        mock_daily_budget,
        store: SQLiteStore,
        tmp_path: Path,
    ) -> None:
        gh = _GitHub()
        settings = _make_settings(TEST_COMMAND="npm test")
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = tmp_path
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.10, iterations=5)
        mock_resolve.return_value = runner

        mock_run_test.return_value = (False, "still failing")
        mock_daily_budget.return_value = (False, "daily_budget_exceeded[x]: 50.00/50.00")

        mock_git.side_effect = ["", " M src/foo.py\n", "", "", ""]

        run_task(store, fetched, settings, gh.plugin(settings))

        assert runner.run.call_count == 1
        testing_notes = gh.pr_section("How I tested")
        assert testing_notes == "- [ ] `npm test` failed (see log)"

    @patch("issue_agent.runner.executor._run_test_command")
    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_no_additional_changes_on_retry_does_not_abort(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        mock_run_test,
        store: SQLiteStore,
        tmp_path: Path,
    ) -> None:
        """A retry iteration that makes no additional edits must not trigger
        the first-iteration `no_changes` abort — there is prior real work
        already committed and pushed. It stops retrying and falls through to
        PR creation instead of spending another attempt."""
        gh = _GitHub()
        settings = _make_settings(TEST_COMMAND="npm test")
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = tmp_path
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.10, iterations=5)
        mock_resolve.return_value = runner

        mock_run_test.return_value = (False, "still failing")

        mock_git.side_effect = [
            "",  # checkout -b
            " M src/foo.py\n",  # status (iteration 1, real edits)
            "",  # add
            "",  # commit
            "",  # push
            "",  # status (iteration 2, retry: clean tree)
            "",  # _salvage_agent_commits: rev-list --count (0 ahead)
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        assert runner.run.call_count == 2
        work = store.get_issue_work("github", "stateway-io/stateway-issues", 42)
        assert work.state == "closed"
        assert work.closed_reason == "pr_opened"  # NOT no_changes

    @patch("issue_agent.runner.executor._run_test_command")
    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_disabled_flag_preserves_current_behavior(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        mock_run_test,
        store: SQLiteStore,
        tmp_path: Path,
    ) -> None:
        gh = _GitHub()
        settings = _make_settings(TEST_COMMAND="npm test", RESUME_RETRY_ON_TEST_FAILURE=False)
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        mock_ws_instance = MagicMock()
        mock_ws_instance.clone.return_value = tmp_path
        mock_ws.return_value = mock_ws_instance

        runner = MagicMock()
        runner.run.return_value = AgentResult(success=True, cost_usd=0.10, iterations=5)
        mock_resolve.return_value = runner

        mock_run_test.return_value = (False, "still failing")

        mock_git.side_effect = ["", " M src/foo.py\n", "", "", ""]

        run_task(store, fetched, settings, gh.plugin(settings))

        assert runner.run.call_count == 1
        testing_notes = gh.pr_section("How I tested")
        assert testing_notes == "- [ ] `npm test` failed (see log)"


class TestIssueWorkLifecycle:
    """The executor opens/reads an `issue_work` record at the start of
    every FIX run, freezes the work branch there, posts an initial comment,
    and closes the record on the run's outcome (R1, R4.1, R8.1)."""

    _GIT_SUCCESS = ["", " M src/foo.py\n", "", "", ""]  # checkout, status, add, commit, push

    def _wire(self, mock_ws, mock_resolve, gh: _GitHub, result: AgentResult):
        ws = MagicMock()
        ws.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = ws
        runner = MagicMock()
        runner.run.return_value = result
        mock_resolve.return_value = runner
        return ws, gh

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_first_run_opens_record_and_posts_one_initial_comment(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        _, pr = self._wire(mock_ws, mock_resolve, gh, AgentResult(True, 0.5, 10))
        mock_git.side_effect = self._GIT_SUCCESS

        run_task(store, fetched, settings, gh.plugin(settings))

        work = store.get_issue_work("github", task.repo, task.issue_number)
        assert work is not None
        assert work.work_branch == "agent/issue-42-fix-broken-link-in-docs"
        assert work.target_repo == task.target_repo
        assert work.base_branch == task.base_branch

        events = [e["type"] for e in _task_events(store, task.id)]
        assert "issue_work_opened" in events

        initial = [c for c in gh.comments() if "agent/issue-42-fix-broken-link-in-docs" in c]
        assert len(initial) == 1
        assert task.id in initial[0]

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_editing_issue_title_between_runs_does_not_move_the_branch(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        store.open_issue_work(
            "github",
            "stateway-io/stateway-issues",
            42,
            "stateway-io/stateway-agent-fixtures",
            "main",
            "agent/issue-42-original-title",
        )
        # the trigger now carries a DIFFERENT title than when the record was opened
        renamed = _TRIGGER.model_copy(
            update={"issue": _ISSUE.model_copy(update={"title": "totally renamed"})}
        )
        task = _make_task(trigger_json=renamed.model_dump_json())
        store.insert_task(task)
        fetched = store.fetch_next()
        _, pr = self._wire(mock_ws, mock_resolve, gh, AgentResult(True, 0.5, 10))
        # resumed run: ls-remote (branch absent) then the normal success flow
        mock_git.side_effect = ["", "", " M src/foo.py\n", "", "", ""]

        run_task(store, fetched, settings, gh.plugin(settings))

        checkout = next(c for c in mock_git.call_args_list if c.args[1:3] == ("checkout", "-b"))
        assert checkout.args[1:4] == ("checkout", "-b", "agent/issue-42-original-title")
        push = mock_git.call_args_list[-1]
        assert "agent/issue-42-original-title" in push.args

        work = store.get_issue_work("github", task.repo, task.issue_number)
        assert work.attempts == 1  # resume bumped it
        events = [e["type"] for e in _task_events(store, task.id)]
        assert "issue_work_resumed" in events

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_resume_comment_mentions_attempt_and_prior_stop(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        store.open_issue_work(
            "github",
            "stateway-io/stateway-issues",
            42,
            "stateway-io/stateway-agent-fixtures",
            "main",
            "agent/issue-42-x",
        )
        store.update_issue_work(
            "github",
            "stateway-io/stateway-issues",
            42,
            last_error="task_timeout",
        )
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        _, pr = self._wire(mock_ws, mock_resolve, gh, AgentResult(True, 0.5, 10))
        mock_git.side_effect = self._GIT_SUCCESS

        run_task(store, fetched, settings, gh.plugin(settings))

        resume = gh.comments()[0]
        assert "attempt 2" in resume.lower()
        assert "execution time" in resume.lower() or "timeout" in resume.lower()

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_initial_comment_suppressed_by_config(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
    ) -> None:
        gh = _GitHub()
        settings = _make_settings(COMMENTS_INITIAL_COMMENT=False)
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        _, pr = self._wire(mock_ws, mock_resolve, gh, AgentResult(True, 0.5, 10))
        mock_git.side_effect = self._GIT_SUCCESS

        run_task(store, fetched, settings, gh.plugin(settings))

        # only the success/fix comment, no initial one
        assert len(gh.comments()) == 1
        assert "PR" in gh.comments()[0]

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_success_closes_record_as_pr_opened(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        self._wire(mock_ws, mock_resolve, gh, AgentResult(True, 0.5, 10))
        mock_git.side_effect = self._GIT_SUCCESS

        run_task(store, fetched, settings, gh.plugin(settings))

        work = store.get_issue_work("github", task.repo, task.issue_number)
        assert work.state == "closed"
        assert work.closed_reason == "pr_opened"
        assert work.pr_number == 101
        # Persisted so a later comment-triggered re-trigger on this issue
        # can inject it as resume_summary instead of starting from zero.
        assert work.last_summary
        assert work.last_summary_kind == "submission"
        events = [e["type"] for e in _task_events(store, task.id)]
        assert "issue_work_closed" in events

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_no_changes_closes_record_as_no_changes(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        _, pr = self._wire(
            mock_ws,
            mock_resolve,
            gh,
            AgentResult(
                True, 0.3, 5, submission="Nothing to do: the linked bug was already fixed in #40."
            ),
        )
        mock_git.side_effect = ["", "", "0"]  # checkout, empty status, rev-list

        run_task(store, fetched, settings, gh.plugin(settings))

        work = store.get_issue_work("github", task.repo, task.issue_number)
        assert work.state == "closed"
        assert work.closed_reason == "no_changes"
        # Persisted (and posted) so a later comment-triggered re-trigger can
        # see *why* no change was made instead of re-deriving it from zero.
        assert work.last_summary == "Nothing to do: the linked bug was already fixed in #40."
        assert work.last_summary_kind == "submission"
        abort_comment = gh.comments()[-1]
        assert "already fixed in #40" in abort_comment

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_block_keeps_record_open_and_records_error(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        self._wire(mock_ws, mock_resolve, gh, AgentResult(False, 2.0, 20, error="budget_exceeded"))
        mock_git.side_effect = ["", "", "0"]

        run_task(store, fetched, settings, gh.plugin(settings))

        work = store.get_issue_work("github", task.repo, task.issue_number)
        assert work.state == "open"
        assert work.last_error == "budget_exceeded"
        assert work.last_task_id == task.id
        assert work.attempts == 0  # bump happens on the next run's resume

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_agent_failure_with_resume_disabled_closes_record(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
    ) -> None:
        """With `resume.enabled: false` nothing is preserved on a failure, so
        there is nothing to resume: the record closes as terminal_failure
        (with resume on, the WIP is pushed and the record stays open)."""
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        self._wire(mock_ws, mock_resolve, gh, AgentResult(False, 0.1, 3, error="agent_crashed"))
        mock_git.side_effect = ["", "", "0"]

        settings = _make_settings(RESUME_ENABLED=False)
        run_task(store, fetched, settings, gh.plugin(settings))

        work = store.get_issue_work("github", task.repo, task.issue_number)
        assert work.state == "closed"
        assert work.closed_reason == "terminal_failure"

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_recoverable_push_failure_leaves_record_open(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        self._wire(mock_ws, mock_resolve, gh, AgentResult(True, 0.5, 10))
        mock_git.side_effect = [
            "",
            " M src/foo.py\n",
            "",
            "",
            RuntimeError("push failed"),
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        assert store.get_task(task.id).status is TaskStatus.FAILED_RECOVERABLE
        work = store.get_issue_work("github", task.repo, task.issue_number)
        assert work.state == "open"  # the retry must resume the same branch


class TestFaseDGuardAndObservability:
    """R7.6 (refuse a run whose issue was closed meanwhile), R8.2
    (audit carries attempts/work_branch), R8.3 (resume counter)."""

    _GIT_SUCCESS = ["", " M src/foo.py\n", "", "", ""]

    def _wire(self, mock_ws, mock_resolve, gh: _GitHub, result: AgentResult):
        ws = MagicMock()
        ws.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = ws
        runner = MagicMock()
        runner.run.return_value = result
        mock_resolve.return_value = runner
        return ws, gh

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_issue_closed_before_run_aborts_without_cost(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        store.open_issue_work(
            "github",
            "stateway-io/stateway-issues",
            42,
            "stateway-io/stateway-agent-fixtures",
            "main",
            "agent/issue-42-x",
        )
        store.close_issue_work("github", "stateway-io/stateway-issues", 42, "issue_closed")
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        ws, pr = self._wire(mock_ws, mock_resolve, gh, AgentResult(True, 0.5, 10))

        run_task(store, fetched, settings, gh.plugin(settings))

        assert store.get_task(task.id).status is TaskStatus.ABORTED_POLICY
        ws.clone.assert_not_called()
        mock_resolve.assert_not_called()
        assert gh.comments() == []
        # the row stays closed / issue_closed — not reopened
        work = store.get_issue_work("github", "stateway-io/stateway-issues", 42)
        assert work.state == "closed"
        assert work.closed_reason == "issue_closed"

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_closed_for_other_reason_still_starts_fresh(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        # a row closed as `max_attempts` (not issue_closed) is a spent work
        # item — a new trigger opens a brand-new one (unchanged rule).
        store.open_issue_work(
            "github",
            "stateway-io/stateway-issues",
            42,
            "stateway-io/stateway-agent-fixtures",
            "main",
            "agent/old",
        )
        store.close_issue_work("github", "stateway-io/stateway-issues", 42, "max_attempts")
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        _, pr = self._wire(mock_ws, mock_resolve, gh, AgentResult(True, 0.5, 10))
        mock_git.side_effect = self._GIT_SUCCESS

        run_task(store, fetched, settings, gh.plugin(settings))

        assert store.get_task(task.id).status is TaskStatus.SUCCEEDED
        work = store.get_issue_work("github", "stateway-io/stateway-issues", 42)
        assert work.state == "closed"
        assert work.closed_reason == "pr_opened"

    @patch("issue_agent.runner.executor.observe_resume")
    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_resume_increments_resume_counter(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        mock_observe_resume,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        store.open_issue_work(
            "github",
            "stateway-io/stateway-issues",
            42,
            "stateway-io/stateway-agent-fixtures",
            "main",
            "agent/issue-42-x",
        )
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        _, pr = self._wire(mock_ws, mock_resolve, gh, AgentResult(True, 0.5, 10))
        # resumed run: ls-remote (absent) then success flow
        mock_git.side_effect = ["", "", " M src/foo.py\n", "", "", ""]

        run_task(store, fetched, settings, gh.plugin(settings))

        mock_observe_resume.assert_called_once_with(
            "stateway-io/stateway-agent-fixtures", provider_instance="github"
        )

    @patch("issue_agent.runner.executor.observe_resume")
    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_first_run_does_not_increment_resume_counter(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        mock_observe_resume,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        _, pr = self._wire(mock_ws, mock_resolve, gh, AgentResult(True, 0.5, 10))
        mock_git.side_effect = self._GIT_SUCCESS

        run_task(store, fetched, settings, gh.plugin(settings))

        mock_observe_resume.assert_not_called()

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_progress_callback_posts_a_progress_comment(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        ws, pr = self._wire(mock_ws, mock_resolve, gh, AgentResult(True, 0.5, 10))
        mock_git.side_effect = self._GIT_SUCCESS

        runner = mock_resolve.return_value

        def _run(
            ws_path,
            t,
            s,
            route,
            resume_summary=None,
            on_progress=None,
            toolchain_env=None,
            branch_status=None,
            comment_history=None,
            terms=None,
        ):
            on_progress(25, 0.33)
            return AgentResult(True, 0.5, 10)

        runner.run.side_effect = _run

        run_task(store, fetched, settings, gh.plugin(settings))

        progress = [c for c in gh.comments() if "is still working" in c]
        assert len(progress) == 1
        assert "Iteration: 25" in progress[0]
        assert "0.33" in progress[0]

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_audit_entry_carries_attempts_and_work_branch(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        _, pr = self._wire(mock_ws, mock_resolve, gh, AgentResult(True, 0.5, 10))
        mock_git.side_effect = self._GIT_SUCCESS

        run_task(store, fetched, settings, gh.plugin(settings))

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        audit_path = Path(settings.data_dir) / f"audit.{today}.log"
        entries = [json.loads(line) for line in audit_path.read_text().strip().split("\n")]
        processed = next(e for e in entries if e["event"] == "task_processed")
        assert processed["work_branch"] == "agent/issue-42-fix-broken-link-in-docs"
        assert processed["attempts"] == 0
        assert processed["provider_instance"] == "github"


class TestResumeAndWipPreservation:
    """On a block the executor commits and pushes the partial work to
    the frozen branch before cleanup, persists the summary, and keeps
    `issue_work` open so the next trigger resumes it (R4.2–R4.8, R5)."""

    _ISSUE_REPO = "stateway-io/stateway-issues"
    _TARGET = "stateway-io/stateway-agent-fixtures"
    _BRANCH = "agent/issue-42-fix-broken-link-in-docs"

    def _wire(self, mock_ws, mock_resolve, gh: _GitHub, result: AgentResult):
        ws = MagicMock()
        ws.clone.return_value = Path("/tmp/ws")
        mock_ws.return_value = ws
        runner = MagicMock()
        runner.run.return_value = result
        mock_resolve.return_value = runner
        return ws, gh, runner

    def _block_result(self, error="task_timeout", **kw):
        return AgentResult(
            success=False,
            cost_usd=1.5,
            iterations=15,
            error=error,
            block_summary="did X, still need Y",
            block_summary_kind="mechanical",
            **kw,
        )

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_block_with_wip_pushes_and_keeps_record_open(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        _, pr, _ = self._wire(mock_ws, mock_resolve, gh, self._block_result())
        mock_git.side_effect = [
            "",  # checkout -b
            "",  # add -A
            " M src/foo.py\n",  # status --porcelain (WIP present)
            "",  # commit
            "",  # push -f
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        assert store.get_task(task.id).status is TaskStatus.ABORTED_BUDGET
        work = store.get_issue_work("github", self._ISSUE_REPO, 42)
        assert work.state == "open"
        assert work.last_summary == "did X, still need Y"
        assert work.last_summary_kind == "mechanical"
        assert work.last_error == "task_timeout"

        pushes = [c for c in mock_git.call_args_list if "push" in c.args]
        assert pushes and self._BRANCH in pushes[0].args
        block_comment = gh.comments()[-1]
        assert self._BRANCH in block_comment
        assert "next trigger" in block_comment.lower()

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_wip_push_authenticates_against_the_configured_github_host(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        settings = with_github(settings, host="https://ghe.example.com")
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        self._wire(mock_ws, mock_resolve, gh, self._block_result())
        mock_git.side_effect = ["", "", " M src/foo.py\n", "", ""]

        run_task(store, fetched, settings, gh.plugin(settings))

        (push,) = [c for c in mock_git.call_args_list if "push" in c.args]
        assert push.kwargs["env"]["GIT_CONFIG_KEY_0"] == (
            "http.https://ghe.example.com.extraHeader"
        )

    def _llm_error_result(self, **kw):
        return AgentResult(
            success=False,
            cost_usd=0.06,
            iterations=49,
            error="llm_error",
            error_detail="APIError: OpenrouterException - [SSL: UNEXPECTED_EOF_WHILE_READING]",
            block_summary="edited foo.py, tests pending",
            block_summary_kind="mechanical",
            **kw,
        )

    _WIP_GIT = [
        "",  # checkout -b
        "",  # add -A
        " M src/foo.py\n",  # status --porcelain (WIP present)
        "",  # commit
        "",  # push -f
    ]

    @patch("issue_agent.runner.executor.send_budget_alert")
    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_llm_error_preserves_wip_and_stays_recoverable(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        mock_alert,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        """Regression: an APIError after 49 iterations used to wipe
        the workspace and close the work as terminal_failure."""
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        ws, pr, _ = self._wire(mock_ws, mock_resolve, gh, self._llm_error_result())
        mock_git.side_effect = list(self._WIP_GIT)

        run_task(store, fetched, settings, gh.plugin(settings))

        updated = store.get_task(task.id)
        assert updated.status is TaskStatus.FAILED_RECOVERABLE
        assert updated.error == "llm_error"
        work = store.get_issue_work("github", self._ISSUE_REPO, 42)
        assert work.state == "open"
        assert work.last_error == "llm_error"
        assert work.last_summary == "edited foo.py, tests pending"

        pushes = [c for c in mock_git.call_args_list if "push" in c.args]
        assert pushes and self._BRANCH in pushes[0].args
        # The workspace is only cleaned up after the push happened.
        ws.cleanup.assert_called_once()
        mock_alert.assert_not_called()  # not a budget event

        comment = gh.comments()[-1]
        assert self._BRANCH in comment
        assert "UNEXPECTED_EOF" in comment
        assert "retried automatically" in comment.lower()
        assert "commenting on the issue resumes" in comment.lower()

    @patch("issue_agent.runner.executor.send_budget_alert")
    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_provider_budget_exceeded_keeps_wip_but_is_terminal_and_alerts(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        mock_alert,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        """OpenRouter budget guardrail: nothing succeeds before the limit is
        raised or resets, so the task is not re-queued — but the WIP is kept
        for the resume and the operator is alerted."""
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        result = AgentResult(
            success=False,
            cost_usd=0.03,
            iterations=3,
            error="provider_budget_exceeded",
            error_detail="Budget limit exceeded (monthly limit).",
            provider_error=ProviderError(
                message="Budget limit exceeded (monthly limit).",
                code=403,
                resets_at="2026-11-01T00:00:00.000Z",
                spent_usd=10.02,
                limit_usd=10.0,
            ),
        )
        _, pr, _ = self._wire(mock_ws, mock_resolve, gh, result)
        mock_git.side_effect = list(self._WIP_GIT)

        run_task(store, fetched, settings, gh.plugin(settings))

        updated = store.get_task(task.id)
        assert updated.status is TaskStatus.FAILED_TERMINAL
        assert updated.error == "provider_budget_exceeded"
        work = store.get_issue_work("github", self._ISSUE_REPO, 42)
        assert work.state == "open"
        assert [c for c in mock_git.call_args_list if "push" in c.args]

        mock_alert.assert_called_once()
        assert mock_alert.call_args.args[1] == "provider_budget_exceeded"
        assert mock_alert.call_args.kwargs["spent_usd"] == 10.02
        assert mock_alert.call_args.kwargs["limit_usd"] == 10.0

        comment = gh.comments()[-1]
        assert "spending limit was reached" in comment
        assert "retried automatically" not in comment.lower()
        assert self._BRANCH in comment

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_llm_error_wip_push_failure_warns_and_stays_recoverable(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        _, pr, _ = self._wire(mock_ws, mock_resolve, gh, self._llm_error_result())
        mock_git.side_effect = [
            "",
            "",
            " M src/foo.py\n",
            "",
            RuntimeError("push rejected"),
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        assert store.get_task(task.id).status is TaskStatus.FAILED_RECOVERABLE
        work = store.get_issue_work("github", self._ISSUE_REPO, 42)
        assert work.state == "open"
        assert work.work_branch == ""
        comment = gh.comments()[-1]
        assert "could not preserve" in comment.lower()

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_llm_error_without_changes_is_terminal_and_closes_no_changes(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        self._wire(mock_ws, mock_resolve, gh, self._llm_error_result())
        mock_git.side_effect = [
            "",  # checkout -b
            "",  # add -A
            "",  # status --porcelain (clean)
            "0\n",  # rev-list --count (not ahead)
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        assert store.get_task(task.id).status is TaskStatus.FAILED_TERMINAL
        assert store.get_issue_work("github", self._ISSUE_REPO, 42).state == "closed"
        assert not [c for c in mock_git.call_args_list if "push" in c.args]

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_agent_crash_preserves_wip_but_does_not_auto_retry(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        """An unclassified crash might be deterministic: keep the work, leave
        `issue_work` open for a human comment, but don't re-queue it."""
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        crash = AgentResult(
            success=False,
            cost_usd=0.2,
            iterations=7,
            error="agent_crashed",
            error_detail="RuntimeError: boom",
        )
        _, pr, _ = self._wire(mock_ws, mock_resolve, gh, crash)
        mock_git.side_effect = list(self._WIP_GIT)

        run_task(store, fetched, settings, gh.plugin(settings))

        assert store.get_task(task.id).status is TaskStatus.FAILED_TERMINAL
        assert store.get_issue_work("github", self._ISSUE_REPO, 42).state == "open"
        assert [c for c in mock_git.call_args_list if "push" in c.args]
        assert "RuntimeError: boom" in gh.comments()[-1]

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_runner_exception_is_converted_and_salvaged(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        """A runner that raises (rather than returning a failed result) must
        not skip the salvage."""
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        _, _, runner = self._wire(mock_ws, mock_resolve, gh, self._llm_error_result())
        runner.run.side_effect = RuntimeError("kaboom")
        mock_git.side_effect = list(self._WIP_GIT)

        run_task(store, fetched, settings, gh.plugin(settings))

        assert store.get_task(task.id).status is TaskStatus.FAILED_TERMINAL
        assert store.get_issue_work("github", self._ISSUE_REPO, 42).state == "open"
        assert [c for c in mock_git.call_args_list if "push" in c.args]

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_block_consumes_sentinel_before_wip_add(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        """The agent's `.agent-test-command` sentinel must be deleted from the
        workspace before `_preserve_wip_on_block`'s `git add -A` ever looks at
        it — otherwise it gets committed and pushed into the target repo's
        branch as a stray dotfile (the bug _consume_test_command_sentinel's
        own docstring warns about). Regression for a real production
        incident: the sentinel was only consumed after the `if not
        result.success` block, i.e. never on a block outcome."""
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        ws, _, _ = self._wire(mock_ws, mock_resolve, gh, self._block_result())
        ws.clone.return_value = tmp_path
        (tmp_path / TEST_COMMAND_SENTINEL_FILENAME).write_text("npm test\n")
        (tmp_path / CHECKS_SENTINEL_FILENAME).write_text("not_run: npm test — timed out\n")
        mock_git.side_effect = [
            "",  # checkout -b
            "",  # add -A
            " M src/foo.py\n",  # status --porcelain (WIP present)
            "",  # commit
            "",  # push -f
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        assert not (tmp_path / TEST_COMMAND_SENTINEL_FILENAME).exists()
        assert not (tmp_path / CHECKS_SENTINEL_FILENAME).exists()

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_resume_accumulates_cost_and_pr_comment_shows_issue_total(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        """End-to-end regression: attempt 1 blocks on
        the iteration limit (cost $0.02, 40 iterations — same numbers as
        task 6d1970e871f84e76ad1b75091d0df573) and attempt 2 resumes,
        succeeds cheaply, and opens the PR. The fix comment must show the
        issue's total spend across both attempts, not just attempt 2's own
        (near-zero) cost — the bug behind task
        bcf38bb3f06e4211ad145d50a0ca8833's "Cost: US$ 0.00" comment."""
        gh = _GitHub()
        task1 = _make_task()
        store.insert_task(task1)
        fetched1 = store.fetch_next()
        self._wire(
            mock_ws,
            mock_resolve,
            gh,
            AgentResult(
                success=False,
                cost_usd=0.02,
                iterations=40,
                error="iteration_limit_exceeded",
                block_summary="hit the step limit",
                block_summary_kind="mechanical",
            ),
        )
        mock_git.side_effect = [
            "",  # checkout -b
            "",  # add -A
            " M src/foo.py\n",  # status --porcelain (WIP present)
            "",  # commit
            "",  # push -f
        ]

        assert fetched1 is not None
        run_task(store, fetched1, settings, gh.plugin(settings))

        work = store.get_issue_work("github", self._ISSUE_REPO, 42)
        assert work.state == "open"
        assert work.total_cost_usd == 0.02
        assert work.total_iterations == 40

        task2 = _make_task()
        store.insert_task(task2)
        fetched2 = store.fetch_next()
        assert fetched2 is not None
        _, pr, _ = self._wire(
            mock_ws,
            mock_resolve,
            gh,
            AgentResult(success=True, cost_usd=0.004, iterations=16, submission="fixed it"),
        )
        mock_git.side_effect = [
            "abc\trefs/heads/x",  # ls-remote (work branch exists on the remote)
            "",  # fetch --unshallow
            "",  # fetch origin <branch>
            "",  # checkout -b <branch> FETCH_HEAD
            "",  # merge origin/<base>
            " M src/foo.py\n",  # status --porcelain (non-empty)
            "",  # add -A
            "",  # commit
            "",  # push -f
        ]

        run_task(store, fetched2, settings, gh.plugin(settings))

        work = store.get_issue_work("github", self._ISSUE_REPO, 42)
        assert work.state == "closed"
        assert work.closed_reason == "pr_opened"
        assert work.total_cost_usd == 0.024
        assert work.total_iterations == 56

        fix_comment = gh.comments()[-1]
        assert "Cost: US$ 0.00" in fix_comment  # attempt 2 alone
        assert "Total accumulated for this issue: US$ 0.02" in fix_comment
        assert "56 iterations across 2 attempts" in fix_comment

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_block_without_changes_closes_no_changes_and_does_not_push(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        _, pr, _ = self._wire(
            mock_ws,
            mock_resolve,
            gh,
            AgentResult(success=False, cost_usd=0.4, iterations=3, error="task_timeout"),
        )
        mock_git.side_effect = [
            "",  # checkout -b
            "",  # add -A
            "",  # status --porcelain (clean)
            "0",  # rev-list --count (not ahead)
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        work = store.get_issue_work("github", self._ISSUE_REPO, 42)
        assert work.state == "closed"
        assert work.closed_reason == "no_changes"
        assert not [c for c in mock_git.call_args_list if "push" in c.args]

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_block_without_changes_persists_block_summary(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        """A block with zero WIP still had a block_summary computed
        (`_maybe_block_summary` runs before the has-WIP check) — it must not
        be silently dropped just because there's nothing to push."""
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        self._wire(
            mock_ws,
            mock_resolve,
            gh,
            AgentResult(
                success=False,
                cost_usd=0.4,
                iterations=3,
                error="task_timeout",
                block_summary="explored the repo, found no matching code path",
                block_summary_kind="mechanical",
            ),
        )
        mock_git.side_effect = [
            "",  # checkout -b
            "",  # add -A
            "",  # status --porcelain (clean)
            "0",  # rev-list --count (not ahead)
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        work = store.get_issue_work("github", self._ISSUE_REPO, 42)
        assert work.state == "closed"
        assert work.closed_reason == "no_changes"
        assert work.last_summary == "explored the repo, found no matching code path"
        assert work.last_summary_kind == "mechanical"

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_wip_push_failure_blanks_branch_keeps_open_and_warns(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        _, pr, _ = self._wire(mock_ws, mock_resolve, gh, self._block_result())
        mock_git.side_effect = [
            "",  # checkout -b
            "",  # add -A
            " M src/foo.py\n",  # status --porcelain
            "",  # commit
            RuntimeError("remote rejected"),  # push -f
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        assert store.get_task(task.id).status is TaskStatus.ABORTED_BUDGET
        work = store.get_issue_work("github", self._ISSUE_REPO, 42)
        assert work.state == "open"
        assert work.work_branch == ""  # R4.4: recompute next run
        warn = gh.comments()[-1]
        assert "could not preserve" in warn.lower()

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_max_attempts_reached_closes_and_comments_giveup(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
    ) -> None:
        gh = _GitHub()
        settings = _make_settings(RESUME_MAX_ATTEMPTS=2)
        store.open_issue_work("github", self._ISSUE_REPO, 42, self._TARGET, "main", self._BRANCH)
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        _, pr, _ = self._wire(mock_ws, mock_resolve, gh, self._block_result())
        mock_git.side_effect = [
            "abc\trefs/heads/x",  # ls-remote (branch exists)
            "",  # fetch --unshallow
            "",  # fetch origin <branch>
            "",  # checkout -b <branch> FETCH_HEAD
            "",  # merge origin/<base>
            "",  # add -A
            " M src/foo.py\n",  # status --porcelain
            "",  # commit
            "",  # push -f
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        work = store.get_issue_work("github", self._ISSUE_REPO, 42)
        assert work.state == "closed"
        assert work.closed_reason == "max_attempts"
        giveup = gh.comments()[-1]
        assert "giving up" in giveup.lower()
        assert self._BRANCH in giveup

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_resume_disabled_does_not_push_but_records_error(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
    ) -> None:
        gh = _GitHub()
        settings = _make_settings(RESUME_ENABLED=False)
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        self._wire(mock_ws, mock_resolve, gh, self._block_result())
        mock_git.side_effect = ["", ""]  # checkout, (defensive) nothing else expected

        run_task(store, fetched, settings, gh.plugin(settings))

        work = store.get_issue_work("github", self._ISSUE_REPO, 42)
        assert work.state == "open"
        assert work.last_error == "task_timeout"
        assert not [c for c in mock_git.call_args_list if "push" in c.args]

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_resume_clones_work_branch_when_present_and_injects_summary(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        store.open_issue_work("github", self._ISSUE_REPO, 42, self._TARGET, "main", self._BRANCH)
        store.update_issue_work(
            "github",
            self._ISSUE_REPO,
            42,
            last_summary="prior: fixed the parser",
            last_error="task_timeout",
        )
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        _, pr, runner = self._wire(
            mock_ws,
            mock_resolve,
            gh,
            AgentResult(True, 0.5, 10),
        )
        mock_git.side_effect = [
            "abc\trefs/heads/x",  # ls-remote → exists
            "",  # fetch --unshallow
            "",  # fetch origin <branch>
            "",  # checkout -b <branch> FETCH_HEAD
            "",  # merge origin/<base>
            " M src/foo.py\n",  # status --porcelain (success path)
            "",  # add
            "",  # commit
            "",  # push
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        git_verbs = [c.args[1] for c in mock_git.call_args_list if len(c.args) > 1]
        assert "ls-remote" in git_verbs
        assert "fetch" in git_verbs
        assert runner.run.call_args.kwargs.get("resume_summary") == "prior: fixed the parser"

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_resume_falls_back_to_checkout_b_when_branch_absent(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        gh = _GitHub()
        store.open_issue_work("github", self._ISSUE_REPO, 42, self._TARGET, "main", self._BRANCH)
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        self._wire(mock_ws, mock_resolve, gh, AgentResult(True, 0.5, 10))
        mock_git.side_effect = [
            "",  # ls-remote → absent
            "",  # checkout -b <branch>
            " M src/foo.py\n",  # status
            "",
            "",
            "",  # add, commit, push
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        git_calls = [c.args[1:] for c in mock_git.call_args_list if len(c.args) > 1]
        assert ("checkout", "-b", self._BRANCH) in git_calls
        assert not any(a[0] == "fetch" for a in git_calls)

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_pr_opened_reopen_continues_from_existing_branch_even_if_resume_disabled(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
    ) -> None:
        """A maintainer's follow-up comment on an issue whose PR is already
        open must not re-clone from scratch and force-push over the
        existing diff — it continues from the PR's own branch and sees what
        the agent said last time, regardless of `resume.enabled` (that flag
        only gates the unrelated block-retry resume machinery; a pr_opened
        reopen is a distinct feature that isn't gated by it)."""
        gh = _GitHub()
        settings = _make_settings(RESUME_ENABLED=False)
        store.open_issue_work("github", self._ISSUE_REPO, 42, self._TARGET, "main", self._BRANCH)
        store.update_issue_work(
            "github",
            self._ISSUE_REPO,
            42,
            last_summary="Opened PR #101 fixing the null check in parser.py",
            last_summary_kind="submission",
        )
        store.close_issue_work("github", self._ISSUE_REPO, 42, "pr_opened", pr_number=101)

        task = _make_task(trigger_kind=TriggerKind.COMMENT_COMMAND)
        store.insert_task(task)
        fetched = store.fetch_next()
        _, pr, runner = self._wire(
            mock_ws,
            mock_resolve,
            gh,
            AgentResult(
                True, 0.1, 4, submission="Also handled the edge case from the new comment."
            ),
        )
        mock_git.side_effect = [
            "abc\trefs/heads/x",  # ls-remote → branch still exists
            "",  # fetch --unshallow
            "",  # fetch origin <branch>
            "",  # checkout -b <branch> FETCH_HEAD
            "",  # merge origin/<base>
            " M src/parser.py\n",  # status --porcelain
            "",  # add
            "",  # commit
            "",  # push
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        git_verbs = [c.args[1] for c in mock_git.call_args_list if len(c.args) > 1]
        assert "ls-remote" in git_verbs
        assert "fetch" in git_verbs
        assert runner.run.call_args.kwargs.get("resume_summary") == (
            "Opened PR #101 fixing the null check in parser.py"
        )

        work = store.get_issue_work("github", self._ISSUE_REPO, 42)
        assert work.work_branch == self._BRANCH  # same branch, not recomputed
        assert work.attempts == 0  # not a block retry — not counted
        assert work.closed_reason == "pr_opened"
        assert work.pr_number == 101

    def _open_pr_followup(self, store: SQLiteStore):
        store.open_issue_work("github", self._ISSUE_REPO, 42, self._TARGET, "main", self._BRANCH)
        store.update_issue_work(
            "github",
            self._ISSUE_REPO,
            42,
            last_summary="Original fix",
            last_summary_kind="submission",
        )
        store.close_issue_work("github", self._ISSUE_REPO, 42, "pr_opened", pr_number=101)
        task = _make_task(trigger_kind=TriggerKind.COMMENT_COMMAND)
        store.insert_task(task)
        return store.fetch_next()

    _CONFLICT = BaseUpdate("conflicted", [(".memory-bank/activeContext.md", "both modified")])

    @patch("issue_agent.runner.executor._abandon_base_merge")
    @patch(
        "issue_agent.runner.executor._finish_base_merge",
        return_value=[".memory-bank/activeContext.md"],
    )
    @patch("issue_agent.runner.executor._update_branch_with_base")
    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_unresolved_base_merge_pushes_nothing_and_keeps_the_pr_followable(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        mock_update,
        mock_finish,
        mock_abandon,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        """Regression: a run that leaves conflict markers must not push
        (nor claim it updated the PR), and the work item must go back to
        `pr_opened` — any other closed reason would make the next comment
        restart from the base and force-push over the PR."""
        gh = _GitHub()
        mock_update.return_value = self._CONFLICT
        fetched = self._open_pr_followup(store)
        _, pr, _ = self._wire(
            mock_ws,
            mock_resolve,
            gh,
            AgentResult(True, 0.05, 42, submission="Resolved the conflicts."),
        )
        mock_git.side_effect = [
            "abc\trefs/heads/x",  # ls-remote
            "",  # fetch --unshallow
            "",  # fetch origin <branch>
            "",  # checkout -b <branch> FETCH_HEAD
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        branch_status = mock_resolve.return_value.run.call_args.kwargs["branch_status"]
        assert "Pull Request #101" in branch_status
        assert ".memory-bank/activeContext.md (both modified)" in branch_status
        assert not [c for c in mock_git.call_args_list if "push" in c.args]
        mock_abandon.assert_called_once()
        updated = store.get_task(fetched.id)
        assert updated.status == TaskStatus.ABORTED_POLICY
        assert updated.error == "merge_conflict_unresolved"
        work = store.get_issue_work("github", self._ISSUE_REPO, 42)
        assert work.closed_reason == "pr_opened"
        assert work.pr_number == 101
        assert work.last_summary == "Original fix"
        gh.pulls.create_pr.assert_not_called()
        comment = gh.comments()[-1]
        assert "`.memory-bank/activeContext.md`" in comment
        assert "Nothing was pushed" in comment

    @patch(
        "issue_agent.runner.executor._changed_files_vs_base", return_value=[("a.txt", "modified")]
    )
    @patch("issue_agent.runner.executor._salvage_agent_commits", return_value="")
    @patch("issue_agent.runner.executor._finish_base_merge", return_value=[])
    @patch("issue_agent.runner.executor._update_branch_with_base")
    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_resolved_base_merge_alone_is_pushed(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        mock_update,
        mock_finish,
        mock_salvage,
        mock_changed,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        """Resolving the conflicts is the whole job of a "update the PR"
        follow-up: with nothing else in the working tree the recorded merge is
        still pushed, not dropped as `no_changes` nor folded by the salvage."""
        gh = _GitHub()
        mock_update.return_value = self._CONFLICT
        fetched = self._open_pr_followup(store)
        _, pr, _ = self._wire(
            mock_ws,
            mock_resolve,
            gh,
            AgentResult(True, 0.05, 42, submission="Resolved the conflicts."),
        )
        gh.pulls.pr_has_conflicts.return_value = True
        mock_git.side_effect = [
            "abc\trefs/heads/x",  # ls-remote
            "",  # fetch --unshallow
            "",  # fetch origin <branch>
            "",  # checkout -b <branch> FETCH_HEAD
            "",  # status --porcelain (merge already recorded)
            "",  # push -f
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        mock_finish.assert_called_once()
        assert [c for c in mock_git.call_args_list if "push" in c.args]
        assert not [c for c in mock_git.call_args_list if "commit" in c.args]
        assert store.get_task(fetched.id).status == TaskStatus.SUCCEEDED
        assert store.get_issue_work("github", self._ISSUE_REPO, 42).closed_reason == "pr_opened"

        # The PR (same number as before: an update, not a new PR) gets the
        # follow-up's report as a comment; its body is never rewritten.
        gh.last_plugin.pulls.assert_called_once_with(self._TARGET)
        pr, pr_comment = gh.pulls.comment_on_pr.call_args.args
        assert pr.number == 101
        assert "Resolved the conflicts." in pr_comment
        assert "- `a.txt` — modified" in pr_comment
        issue_comment = gh.comments()[-1]
        assert "updated the PR" in issue_comment
        assert "still reports conflicts" in issue_comment

    @patch("issue_agent.runner.executor._preserve_wip_on_block")
    @patch("issue_agent.runner.executor._abandon_base_merge")
    @patch("issue_agent.runner.executor._finish_base_merge", return_value=["a.txt"])
    @patch("issue_agent.runner.executor._update_branch_with_base")
    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_block_backs_out_of_an_unresolved_base_merge_before_the_wip_commit(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        mock_update,
        mock_finish,
        mock_abandon,
        mock_wip,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        """The WIP commit would otherwise conclude the merge, markers and all."""
        gh = _GitHub()
        mock_update.return_value = self._CONFLICT
        fetched = self._open_pr_followup(store)
        self._wire(mock_ws, mock_resolve, gh, self._block_result())
        mock_git.side_effect = ["abc\trefs/heads/x", "", "", ""]
        order = MagicMock()
        mock_abandon.side_effect = lambda *a: order("abandon")
        mock_wip.side_effect = lambda *a, **k: (
            order("wip"),
            WipOutcome(reason="pushed", branch=self._BRANCH),
        )[1]

        run_task(store, fetched, settings, gh.plugin(settings))

        assert [c.args[0] for c in order.call_args_list] == ["abandon", "wip"]

    @patch("issue_agent.runner.executor._run_git")
    @patch("issue_agent.runner.executor.Workspace")
    @patch("issue_agent.runner.executor._resolve_agent_runner")
    def test_no_changes_reopen_still_starts_fresh_unlike_pr_opened(
        self,
        mock_resolve,
        mock_ws,
        mock_git,
        store: SQLiteStore,
        settings: Settings,
    ) -> None:
        """Only a `pr_opened` closure gets the continue-from-branch
        treatment. `no_changes` (and, by the same code path,
        `max_attempts`/`terminal_failure`) keep today's fresh-start
        behavior: no remote checkout, no resume_summary injected."""
        gh = _GitHub()
        store.open_issue_work("github", self._ISSUE_REPO, 42, self._TARGET, "main", self._BRANCH)
        store.update_issue_work("github", self._ISSUE_REPO, 42, last_summary="won't be reused")
        store.close_issue_work("github", self._ISSUE_REPO, 42, "no_changes")

        task = _make_task(trigger_kind=TriggerKind.COMMENT_COMMAND)
        store.insert_task(task)
        fetched = store.fetch_next()
        _, pr, runner = self._wire(
            mock_ws,
            mock_resolve,
            gh,
            AgentResult(True, 0.1, 4, submission="fixed it now"),
        )
        mock_git.side_effect = [
            "",  # checkout -b <branch> (fresh — no ls-remote/fetch)
            " M src/foo.py\n",  # status --porcelain
            "",
            "",
            "",  # add, commit, push
        ]

        run_task(store, fetched, settings, gh.plugin(settings))

        git_verbs = [c.args[1] for c in mock_git.call_args_list if len(c.args) > 1]
        assert "ls-remote" not in git_verbs
        assert "fetch" not in git_verbs
        assert runner.run.call_args.kwargs.get("resume_summary") is None
        assert store.get_issue_work("github", self._ISSUE_REPO, 42).attempts == 0
        # A fresh run opens its own PR: nothing to report on an existing one.
        gh.pulls.comment_on_pr.assert_not_called()


class TestMiniSWERunner:
    """Tests for MiniSWERunner cost/iteration tracking (13.4.9)."""

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_reads_cost_and_n_calls_after_run(self, mock_build_agent: MagicMock) -> None:
        from issue_agent.runner.executor import MiniSWERunner

        task = _make_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None

        mock_agent = MagicMock()
        mock_agent.cost = 1.23
        mock_agent.n_calls = 15
        mock_agent.run.return_value = {"exit_status": "Submitted", "submission": "done"}
        mock_build_agent.return_value = mock_agent

        runner = MiniSWERunner()
        result = runner.run("/ws", task, settings, route, terms=TERMS)

        assert result.success is True
        assert result.cost_usd == 1.23
        assert result.iterations == 15
        assert result.submission == "done"
        mock_agent.run.assert_called_once()

    @pytest.mark.parametrize(
        "run_effect",
        [
            {"return_value": {"exit_status": "Submitted", "submission": "done"}},
            {"return_value": {"exit_status": "LimitsExceeded", "submission": ""}},
            {"side_effect": RuntimeError("boom")},
        ],
        ids=["submitted", "limits_exceeded", "crashed"],
    )
    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_reaps_the_agents_leftover_processes_on_every_exit_path(
        self, mock_build_agent: MagicMock, run_effect: dict
    ) -> None:
        """Whatever ended the run, nothing the agent detached may outlive it."""
        from issue_agent.runner.executor import MiniSWERunner

        task = _make_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None

        mock_agent = MagicMock()
        mock_agent.cost = 0.1
        mock_agent.n_calls = 2
        mock_agent.config = MagicMock(step_limit=20, cost_limit=2.0)
        mock_agent.run.configure_mock(**run_effect)
        mock_build_agent.return_value = mock_agent

        MiniSWERunner().run("/ws", task, settings, route, terms=TERMS)

        mock_agent.env.reap.assert_called_once_with()

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_a_failing_reap_never_breaks_the_run(self, mock_build_agent: MagicMock) -> None:
        from issue_agent.runner.executor import MiniSWERunner

        task = _make_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None

        mock_agent = MagicMock()
        mock_agent.cost = 0.1
        mock_agent.n_calls = 2
        mock_agent.run.return_value = {"exit_status": "Submitted", "submission": "done"}
        mock_agent.env.reap.side_effect = OSError("no /proc")
        mock_build_agent.return_value = mock_agent

        result = MiniSWERunner().run("/ws", task, settings, route, terms=TERMS)

        assert result.success is True

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_time_exceeded_exit_status_maps_to_task_timeout(
        self, mock_build_agent: MagicMock
    ) -> None:
        """A5/A6: DefaultAgent.run() enforces wall_time_limit_seconds natively
        (mini-swe-agent >= 2.3.0) and returns exit_status="TimeExceeded"
        instead of raising. MiniSWERunner must consume that return value."""
        from issue_agent.runner.executor import MiniSWERunner

        task = _make_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None

        mock_agent = MagicMock()
        mock_agent.cost = 0.30
        mock_agent.n_calls = 3
        mock_agent.run.return_value = {"exit_status": "TimeExceeded", "submission": ""}
        mock_build_agent.return_value = mock_agent

        runner = MiniSWERunner()
        result = runner.run("/ws", task, settings, route, terms=TERMS)

        assert result.success is False
        assert result.error == "task_timeout"
        assert result.cost_usd == 0.30
        assert result.iterations == 3

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_limits_exceeded_exit_status_maps_to_budget_exceeded_when_ambiguous(
        self, mock_build_agent: MagicMock
    ) -> None:
        """A5: agent.run() returning exit_status="LimitsExceeded" (cost_limit
        or step_limit hit) must not be reported as success. When both the
        step and cost limits are hit on the same call (or neither counter is
        configured), the generic "budget_exceeded" code is kept as a
        fallback — there's no way to tell which one actually fired."""
        from issue_agent.runner.executor import MiniSWERunner

        task = _make_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None

        mock_agent = MagicMock()
        mock_agent.cost = 2.00
        mock_agent.n_calls = 20
        mock_agent.config = MagicMock(step_limit=20, cost_limit=2.00)
        mock_agent.run.return_value = {"exit_status": "LimitsExceeded", "submission": ""}
        mock_build_agent.return_value = mock_agent

        runner = MiniSWERunner()
        result = runner.run("/ws", task, settings, route, terms=TERMS)

        assert result.success is False
        assert result.error == "budget_exceeded"
        assert result.cost_usd == 2.00
        assert result.iterations == 20

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_limits_exceeded_maps_to_iteration_limit_exceeded_when_only_steps_hit(
        self, mock_build_agent: MagicMock
    ) -> None:
        """Regression for a mislabeled abort
        (task 6d1970e871f84e76ad1b75091d0df573): the step limit fired at
        iteration 40 while cost sat at $0.02, far under the $2 cost limit —
        the abort comment must say "iterations", not "budget"."""
        from issue_agent.runner.executor import MiniSWERunner

        task = _make_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None

        mock_agent = MagicMock()
        mock_agent.cost = 0.02
        mock_agent.n_calls = 40
        mock_agent.config = MagicMock(step_limit=40, cost_limit=2.00)
        mock_agent.run.return_value = {"exit_status": "LimitsExceeded", "submission": ""}
        mock_build_agent.return_value = mock_agent

        runner = MiniSWERunner()
        result = runner.run("/ws", task, settings, route, terms=TERMS)

        assert result.success is False
        assert result.error == "iteration_limit_exceeded"
        assert result.cost_usd == 0.02
        assert result.iterations == 40

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_limits_exceeded_maps_to_cost_limit_exceeded_when_only_cost_hit(
        self, mock_build_agent: MagicMock
    ) -> None:
        from issue_agent.runner.executor import MiniSWERunner

        task = _make_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None

        mock_agent = MagicMock()
        mock_agent.cost = 2.00
        mock_agent.n_calls = 5
        mock_agent.config = MagicMock(step_limit=50, cost_limit=2.00)
        mock_agent.run.return_value = {"exit_status": "LimitsExceeded", "submission": ""}
        mock_build_agent.return_value = mock_agent

        runner = MiniSWERunner()
        result = runner.run("/ws", task, settings, route, terms=TERMS)

        assert result.success is False
        assert result.error == "cost_limit_exceeded"
        assert result.cost_usd == 2.00
        assert result.iterations == 5

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_repeated_format_error_maps_to_format_error(self, mock_build_agent: MagicMock) -> None:
        """DefaultAgent gives up after max_consecutive_format_errors (3)
        unreadable responses in a row. Left unmapped, that status reached the
        metrics and the issue comment raw."""
        from issue_agent.runner.executor import MiniSWERunner

        task = _make_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None

        mock_agent = MagicMock()
        mock_agent.cost = 0.15
        mock_agent.n_calls = 4
        mock_agent.run.return_value = {"exit_status": "RepeatedFormatError", "submission": ""}
        mock_build_agent.return_value = mock_agent

        result = MiniSWERunner().run("/ws", task, settings, route, terms=TERMS)

        assert result.success is False
        assert result.error == "format_error"

    def test_format_error_is_not_a_block_outcome(self) -> None:
        """_BLOCK_ERRORS drives WIP preservation and resume summarization —
        deliberately unchanged here."""
        from issue_agent.runner.executor import _BLOCK_ERRORS

        assert "format_error" not in _BLOCK_ERRORS

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_unknown_exit_status_is_reported_as_error(self, mock_build_agent: MagicMock) -> None:
        """An exit status with no entry in _EXIT_STATUS_ERROR passes through
        verbatim. (This used RepeatedFormatError until that status was mapped
        to "format_error" — see the test above.)"""
        from issue_agent.runner.executor import MiniSWERunner

        task = _make_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None

        mock_agent = MagicMock()
        mock_agent.cost = 0.05
        mock_agent.n_calls = 4
        mock_agent.run.return_value = {"exit_status": "SomeNewUpstreamStatus", "submission": ""}
        mock_build_agent.return_value = mock_agent

        runner = MiniSWERunner()
        result = runner.run("/ws", task, settings, route, terms=TERMS)

        assert result.success is False
        assert result.error == "SomeNewUpstreamStatus"

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_cost_unavailable_error_maps_to_cost_unavailable(
        self, mock_build_agent: MagicMock
    ) -> None:
        """R5.5.3: a strict-mode CostUnavailableError is distinct from a
        generic exception — it must surface as error='cost_unavailable' so the
        executor aborts the task and alerts, rather than retrying it."""
        from issue_agent.runner.executor import MiniSWERunner
        from issue_agent.runner.litellm_cost import CostUnavailableError

        task = _make_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None

        mock_agent = MagicMock()
        mock_agent.cost = 0.12
        mock_agent.n_calls = 2
        mock_agent.run.side_effect = CostUnavailableError("no price map entry")
        mock_build_agent.return_value = mock_agent

        result = MiniSWERunner().run("/ws", task, settings, route, terms=TERMS)

        assert result.success is False
        assert result.error == "cost_unavailable"
        assert result.cost_usd == 0.12
        assert result.iterations == 2

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_captures_partial_cost_on_exception(self, mock_build_agent: MagicMock) -> None:
        from issue_agent.runner.executor import MiniSWERunner

        task = _make_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None

        mock_agent = MagicMock()
        mock_agent.cost = 0.45
        mock_agent.n_calls = 7
        mock_agent.run.side_effect = RuntimeError("something broke")
        mock_build_agent.return_value = mock_agent

        runner = MiniSWERunner()
        result = runner.run("/ws", task, settings, route, terms=TERMS)

        assert result.success is False
        assert result.cost_usd == 0.45
        assert result.iterations == 7
        assert result.error == "agent_crashed"
        assert "something broke" in (result.error_detail or "")
        assert result.submission is None

    @pytest.mark.parametrize(
        "exc_factory",
        [
            lambda litellm: litellm.exceptions.APIError(
                status_code=500,
                message="OpenrouterException - [SSL: UNEXPECTED_EOF_WHILE_READING]",
                llm_provider="openrouter",
                model="m",
            ),
            lambda litellm: litellm.exceptions.InternalServerError(
                message="boom", llm_provider="openrouter", model="m"
            ),
            lambda litellm: litellm.exceptions.APIConnectionError(
                message="conn reset", llm_provider="openrouter", model="m"
            ),
            lambda litellm: litellm.exceptions.Timeout(
                message="timed out", llm_provider="openrouter", model="m"
            ),
            lambda litellm: litellm.exceptions.RateLimitError(
                message="slow down", llm_provider="openrouter", model="m"
            ),
            lambda litellm: __import__("ssl").SSLError("EOF occurred in violation of protocol"),
            lambda litellm: ConnectionResetError("reset by peer"),
        ],
    )
    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_transient_llm_failure_is_classified_llm_error(
        self, mock_build_agent: MagicMock, exc_factory
    ) -> None:
        import litellm

        from issue_agent.runner.executor import MiniSWERunner

        task = _make_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None

        mock_agent = MagicMock()
        mock_agent.cost = 0.06
        mock_agent.n_calls = 49
        mock_agent.run.side_effect = exc_factory(litellm)
        mock_build_agent.return_value = mock_agent

        result = MiniSWERunner().run("/ws", task, settings, route, terms=TERMS)

        assert result.success is False
        assert result.error == "llm_error"
        assert result.cost_usd == 0.06
        assert result.iterations == 49
        assert result.error_detail  # class name + message, for the comment

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_non_transient_llm_failure_is_not_llm_error(self, mock_build_agent: MagicMock) -> None:
        """Auth/not-found style errors are abort_exceptions in mini-swe-agent:
        retrying them automatically would just burn the retry budget."""
        import litellm

        from issue_agent.runner.executor import MiniSWERunner

        task = _make_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None

        mock_agent = MagicMock()
        mock_agent.cost = 0.0
        mock_agent.n_calls = 1
        mock_agent.run.side_effect = litellm.exceptions.AuthenticationError(
            message="bad key", llm_provider="openrouter", model="m"
        )
        mock_build_agent.return_value = mock_agent

        result = MiniSWERunner().run("/ws", task, settings, route, terms=TERMS)

        assert result.error == "agent_crashed"
        assert "AuthenticationError" in (result.error_detail or "")

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_provider_budget_refusal_is_its_own_terminal_error(
        self, mock_build_agent: MagicMock
    ) -> None:
        """The model wrapper turns OpenRouter's
        budget guardrail into ProviderBudgetExceededError; it must not be
        classified as a transient `llm_error` (which gets re-queued)."""
        from issue_agent.runner.executor import MiniSWERunner
        from issue_agent.runner.llm_errors import ProviderBudgetExceededError

        task = _make_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None

        error = ProviderError(
            message="Budget limit exceeded (monthly limit).",
            code=403,
            remedy_hint="Wait for the budget to reset.",
            spent_usd=10.02,
            limit_usd=10.0,
        )
        mock_agent = MagicMock()
        mock_agent.cost = 0.0
        mock_agent.n_calls = 1
        mock_agent.run.side_effect = ProviderBudgetExceededError(error)
        mock_build_agent.return_value = mock_agent

        result = MiniSWERunner().run("/ws", task, settings, route, terms=TERMS)

        assert result.error == "provider_budget_exceeded"
        assert result.provider_error == error
        assert result.error_detail == (
            "Budget limit exceeded (monthly limit). — Wait for the budget to reset."
        )

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_unwrapped_budget_api_error_is_not_transient(self, mock_build_agent: MagicMock) -> None:
        """Belt and braces for a path that bypasses the model wrapper: the raw
        APIError must not be re-queued as transient, and its detail is the
        provider's message rather than a JSON dump cut mid-string."""
        import litellm

        from issue_agent.runner.executor import MiniSWERunner

        task = _make_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None

        body = json.dumps(
            {
                "error": {
                    "message": "Budget limit exceeded (monthly limit).",
                    "code": 403,
                    "metadata": {"limit_source": "openrouter_guardrail_budget"},
                }
            }
        )
        mock_agent = MagicMock()
        mock_agent.cost = 0.0
        mock_agent.n_calls = 1
        mock_agent.run.side_effect = litellm.exceptions.APIError(
            status_code=500,
            message=f"OpenrouterException - {body}",
            llm_provider="openrouter",
            model="m",
        )
        mock_build_agent.return_value = mock_agent

        result = MiniSWERunner().run("/ws", task, settings, route, terms=TERMS)

        assert result.error == "agent_crashed"
        assert result.error_detail == "Budget limit exceeded (monthly limit)."

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_build_agent_failure_does_not_escape(self, mock_build_agent: MagicMock) -> None:
        from issue_agent.runner.executor import MiniSWERunner

        task = _make_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None
        mock_build_agent.side_effect = RuntimeError("template exploded")

        result = MiniSWERunner().run("/ws", task, settings, route, terms=TERMS)

        assert result.success is False
        assert result.error == "agent_crashed"
        assert "template exploded" in (result.error_detail or "")

    @patch("issue_agent.runner.executor._maybe_block_summary")
    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_llm_error_gets_a_block_summary(
        self, mock_build_agent: MagicMock, mock_summary: MagicMock
    ) -> None:
        import litellm

        from issue_agent.runner.executor import MiniSWERunner

        task = _make_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)
        assert route is not None

        mock_agent = MagicMock()
        mock_agent.cost = 0.1
        mock_agent.n_calls = 3
        mock_agent.run.side_effect = litellm.exceptions.InternalServerError(
            message="boom", llm_provider="openrouter", model="m"
        )
        mock_build_agent.return_value = mock_agent
        mock_summary.return_value = ("did X", "mechanical", 0.0)

        result = MiniSWERunner().run("/ws", task, settings, route, terms=TERMS)

        assert result.block_summary == "did X"
        assert result.block_summary_kind == "mechanical"
        assert mock_summary.call_args.args[3] == "llm_error"


class TestMiniSWERunnerBlockSummary:
    """R5: on a block outcome the runner produces a summary of the
    interrupted attempt and attaches it to the AgentResult, so the executor can
    persist it and push the WIP before cleanup."""

    def _git(self, path: Path, *args: str) -> None:
        import subprocess

        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)

    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        self._git(tmp_path, "init", "-q")
        self._git(tmp_path, "config", "user.email", "t@t.t")
        self._git(tmp_path, "config", "user.name", "t")
        (tmp_path / "a.py").write_text("x\n")
        self._git(tmp_path, "add", "-A")
        self._git(tmp_path, "commit", "-qm", "base")
        (tmp_path / "a.py").write_text("edited by agent\n")
        return tmp_path

    def _agent(self, exit_status: str, cost: float = 0.30, n_calls: int = 3):
        agent = MagicMock()
        agent.cost = cost
        agent.n_calls = n_calls
        # step_limit/cost_limit both equal to the counters they gate: with
        # both "hit" at once, _limits_exceeded_error can't tell which one
        # actually fired and falls back to the generic "budget_exceeded" —
        # matching this helper's callers, which don't care which specific
        # limit tripped.
        agent.config = MagicMock(step_limit=n_calls, cost_limit=cost)
        agent.extra_template_vars = {}
        agent.messages = [{"role": "assistant", "content": "I edited a.py to fix it"}]
        agent.run.return_value = {"exit_status": exit_status, "submission": ""}
        return agent

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_block_carries_mechanical_summary(self, mock_build, repo: Path) -> None:
        from issue_agent.runner.executor import MiniSWERunner

        agent = self._agent("TimeExceeded")
        mock_build.return_value = agent
        settings = _make_settings()
        task = _make_task()
        route = settings.route_for(task.provider_instance, task.repo)

        result = MiniSWERunner().run(str(repo), task, settings, route, terms=TERMS)

        assert result.error == "task_timeout"
        assert result.block_summary_kind == "mechanical"
        assert "a.py" in result.block_summary
        assert "edited a.py" in result.block_summary

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_success_has_no_block_summary(self, mock_build, repo: Path) -> None:
        from issue_agent.runner.executor import MiniSWERunner

        agent = self._agent("Submitted")
        mock_build.return_value = agent
        settings = _make_settings()
        task = _make_task()
        route = settings.route_for(task.provider_instance, task.repo)

        result = MiniSWERunner().run(str(repo), task, settings, route, terms=TERMS)

        assert result.success is True
        assert result.block_summary is None
        assert result.block_summary_kind == ""

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_llm_summary_cost_folds_into_result_cost(self, mock_build, repo: Path) -> None:
        from issue_agent.runner.executor import MiniSWERunner

        agent = self._agent("TimeExceeded", cost=0.30)
        agent.model.query.return_value = {"content": "what remains: tests", "extra": {"cost": 0.05}}
        mock_build.return_value = agent
        settings = _make_settings(RESUME_SUMMARY_ON_BLOCK="llm")
        task = _make_task()
        route = settings.route_for(task.provider_instance, task.repo)

        result = MiniSWERunner().run(str(repo), task, settings, route, terms=TERMS)

        assert result.block_summary_kind == "llm"
        assert "what remains" in result.block_summary
        assert result.cost_usd == pytest.approx(0.35)

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_budget_block_on_default_policy_spends_no_extra_tokens(
        self, mock_build, repo: Path
    ) -> None:
        """PRD §10 regression 4: a budget-blocked task must not spend a single
        extra token on the summary under the default `mechanical` policy."""
        from issue_agent.runner.executor import MiniSWERunner

        agent = self._agent("LimitsExceeded", cost=2.00)
        mock_build.return_value = agent
        settings = _make_settings()  # resume_summary_on_block defaults to mechanical
        task = _make_task()
        route = settings.route_for(task.provider_instance, task.repo)

        result = MiniSWERunner().run(str(repo), task, settings, route, terms=TERMS)

        assert result.error == "budget_exceeded"
        assert result.block_summary_kind == "mechanical"
        assert result.cost_usd == 2.00  # unchanged — no summary call
        agent.model.query.assert_not_called()

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_on_progress_callback_is_handed_to_build_agent(self, mock_build, repo: Path) -> None:
        """R6: the runner forwards the executor's progress callback
        straight to build_agent, which decides whether to wrap the agent."""
        from issue_agent.runner.executor import MiniSWERunner

        agent = self._agent("Submitted")
        mock_build.return_value = agent
        settings = _make_settings()
        task = _make_task()
        route = settings.route_for(task.provider_instance, task.repo)
        cb = lambda i, c: None  # noqa: E731

        MiniSWERunner().run(str(repo), task, settings, route, terms=TERMS, on_progress=cb)

        assert mock_build.call_args.kwargs["on_progress"] is cb

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_resume_disabled_skips_summary(self, mock_build, repo: Path) -> None:
        from issue_agent.runner.executor import MiniSWERunner

        agent = self._agent("TimeExceeded")
        mock_build.return_value = agent
        settings = _make_settings(RESUME_ENABLED=False)
        task = _make_task()
        route = settings.route_for(task.provider_instance, task.repo)

        result = MiniSWERunner().run(str(repo), task, settings, route, terms=TERMS)

        assert result.error == "task_timeout"
        assert result.block_summary is None

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_prior_resume_summary_injected_into_template_vars(self, mock_build, repo: Path) -> None:
        from issue_agent.runner.executor import MiniSWERunner

        agent = self._agent("Submitted")
        mock_build.return_value = agent
        settings = _make_settings()
        task = _make_task()
        route = settings.route_for(task.provider_instance, task.repo)

        MiniSWERunner().run(
            str(repo), task, settings, route, terms=TERMS, resume_summary="prior handoff note"
        )

        assert agent.extra_template_vars["resume_summary"] == "prior handoff note"

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_branch_status_injected_into_template_vars(self, mock_build, repo: Path) -> None:
        from issue_agent.runner.executor import MiniSWERunner

        agent = self._agent("Submitted")
        mock_build.return_value = agent
        settings = _make_settings()
        task = _make_task()
        route = settings.route_for(task.provider_instance, task.repo)

        MiniSWERunner().run(
            str(repo), task, settings, route, terms=TERMS, branch_status="merge in progress"
        )

        assert agent.extra_template_vars["branch_status"] == "merge in progress"


class TestMiniSWERunnerCommentContext:
    """R3: the triggering comment and the filtered discussion thread reach
    the model through agent.extra_template_vars["comments"]."""

    _COMMENT_TRIGGER = make_trigger_event(
        TriggerKind.COMMENT_COMMAND,
        issue=_ISSUE.model_copy(update={"author": make_author(login="reporter")}),
        trigger_comment=make_comment(
            id="900",
            author=make_author(login="reporter", association="none"),
            body="actually the bug is in the link checker",
            created_at="2026-09-08T10:00:00Z",
        ),
    )

    def _comment_task(self, **overrides):
        return _make_task(
            trigger_kind=TriggerKind.COMMENT_COMMAND,
            trigger_json=self._COMMENT_TRIGGER.model_dump_json(),
            **overrides,
        )

    def _mock_agent(self):
        agent = MagicMock()
        agent.cost = 0.1
        agent.n_calls = 2
        agent.extra_template_vars = {}
        agent.run.return_value = {"exit_status": "Submitted", "submission": "done"}
        return agent

    def test_history_comes_from_the_issue_forge(self) -> None:
        from issue_agent.runner.executor import _fetch_comment_history

        forge = MagicMock(spec=ForgeClient)
        forge.list_comments.return_value = [make_comment(id="1")]
        settings = _make_settings()

        history = _fetch_comment_history(self._comment_task(), settings, forge)

        assert [c.id for c in history] == ["1"]
        forge.list_comments.assert_called_once_with(42, settings.comments_context_max_comments)

    def test_history_fetch_failure_is_best_effort(self) -> None:
        from issue_agent.runner.executor import _fetch_comment_history

        forge = MagicMock(spec=ForgeClient)
        forge.list_comments.side_effect = RuntimeError("forge down")

        assert _fetch_comment_history(self._comment_task(), _make_settings(), forge) == []

    def test_issue_opened_trigger_does_not_fetch_history(self) -> None:
        from issue_agent.runner.executor import _fetch_comment_history

        forge = MagicMock(spec=ForgeClient)

        assert _fetch_comment_history(_make_task(), _make_settings(), forge) == []
        forge.list_comments.assert_not_called()

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_trigger_and_history_reach_the_agent(self, mock_build_agent) -> None:
        from issue_agent.runner.executor import MiniSWERunner

        agent = self._mock_agent()
        mock_build_agent.return_value = agent
        history = [
            make_comment(
                id="1",
                author=make_author(login="maintainer", association="member"),
                body="confirmed, reproduces on main",
            ),
        ]

        task = self._comment_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)

        MiniSWERunner().run("/ws", task, settings, route, terms=TERMS, comment_history=history)

        injected = agent.extra_template_vars["comments"]
        assert "actually the bug is in the link checker" in injected
        assert "triggered this run" in injected
        assert "confirmed, reproduces on main" in injected

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_without_history_the_trigger_comment_still_arrives(self, mock_build_agent) -> None:
        from issue_agent.runner.executor import MiniSWERunner

        agent = self._mock_agent()
        mock_build_agent.return_value = agent

        task = self._comment_task()
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)

        result = MiniSWERunner().run("/ws", task, settings, route, terms=TERMS)

        assert result.success is True
        assert "actually the bug is in the link checker" in agent.extra_template_vars["comments"]

    @patch("issue_agent.runner.agent_factory.build_agent")
    def test_issue_opened_without_history_injects_nothing(self, mock_build_agent) -> None:
        from issue_agent.runner.executor import MiniSWERunner

        agent = self._mock_agent()
        mock_build_agent.return_value = agent

        task = _make_task()  # ISSUE_OPENED, no comment in payload
        settings = _make_settings()
        route = settings.route_for(task.provider_instance, task.repo)

        MiniSWERunner().run("/ws", task, settings, route, terms=TERMS)

        assert agent.extra_template_vars["comments"] == ""


class TestOutbound:
    """Every body the executor sends passes through `_Outbound`, cut to the
    forge's limit; a PR already open on the branch is reused, not recreated."""

    def test_issue_comments_are_cut_to_the_forge_limit(self) -> None:
        from issue_agent.runner.executor import _Outbound

        plugin = FakeProviderWithPulls("fake", secret="s")
        out = _Outbound.for_task(plugin, _make_task())

        out.comment("x" * 5_000)

        [posted] = plugin.forge.posted("stateway-io/stateway-issues", 42)
        assert len(posted) == plugin.capabilities.max_body_length

    def test_an_open_pr_on_the_branch_is_reused(self) -> None:
        from issue_agent.runner.executor import _Outbound

        plugin = FakeProviderWithPulls("fake", secret="s")
        out = _Outbound.for_task(plugin, _make_task())
        fields = dict(head="b", base="main", title="t", body="x", draft=False, labels=[])

        first = out.open_pr(**fields)
        second = out.open_pr(**fields)

        assert second == first
        assert len(plugin.forge.prs["stateway-io/stateway-agent-fixtures"]) == 1

    def test_native_drafts_keep_the_title(self) -> None:
        from issue_agent.runner.executor import _Outbound

        gh = _GitHub()
        out = _Outbound.for_task(gh.plugin(_make_settings()), _make_task())

        out.open_pr(head="b", base="main", title="t", body="x", draft=True, labels=[])

        assert gh.pulls.create_pr.call_args.kwargs["title"] == "t"
        assert gh.pulls.create_pr.call_args.kwargs["draft"] is True

    def test_the_delivery_follows_the_pull_request_api(self) -> None:
        from issue_agent.runner.executor import _Outbound

        with_prs = _Outbound.for_task(FakeProviderWithPulls("fake", secret="s"), _make_task())
        without = _Outbound.for_task(FakeProvider("fake", secret="s"), _make_task())

        assert with_prs.delivery_reason == "pr_opened"
        assert without.delivery_reason == "branch_published"
        assert without.compare_url("agent/x") == (
            "https://forge.example/stateway-io/stateway-agent-fixtures/-/compare/main...agent/x"
        )

    def test_issue_labels_only_where_the_forge_has_labels(self) -> None:
        from issue_agent.runner.executor import _Outbound

        class _NoLabels(FakeProvider):
            capabilities = FakeProvider.capabilities.model_copy(update={"supports_labels": False})

        labelled = FakeProvider("fake", secret="s")
        unlabelled = _NoLabels("fake", secret="s")

        _Outbound.for_task(labelled, _make_task()).label_issue(["agent-generated"])
        _Outbound.for_task(unlabelled, _make_task()).label_issue(["agent-generated"])

        key = ("stateway-io/stateway-issues", 42)
        assert labelled.forge.labels[key] == ["agent-generated"]
        assert key not in unlabelled.forge.labels


def _git_repo(path: Path) -> Path:
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)

    path.mkdir()
    git("init", "-b", "main")
    git("config", "user.email", "bot@test.com")
    git("config", "user.name", "Test Bot")
    (path / "docs.md").write_text("alpha\nbeta\ngamma\n")
    git("add", "-A")
    git("commit", "-m", "initial")
    return path


def _commit(path: Path, content: str, message: str) -> None:
    import subprocess

    (path / "docs.md").write_text(content)
    subprocess.run(["git", "commit", "-am", message], cwd=path, check=True, capture_output=True)


def _checkout(path: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", "checkout", *args], cwd=path, check=True, capture_output=True)


def _set_origin_base(path: Path) -> None:
    """Point ``origin/main`` at the repo's own ``main``, as a clone has it."""
    import subprocess

    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "main"],
        cwd=path,
        check=True,
        capture_output=True,
    )


class TestLocalConflicts:
    """`git merge-tree` in the workspace: the conflict check on a forge with
    no PR API, and the fallback when the forge can't say (PRD §3.7)."""

    def test_a_branch_that_merges_cleanly(self, tmp_path: Path) -> None:
        from issue_agent.runner.executor import _local_conflicts

        repo = _git_repo(tmp_path / "r")
        _checkout(repo, "-b", "work")
        (repo / "other.md").write_text("new\n")
        import subprocess

        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "other"], cwd=repo, check=True, capture_output=True)
        _set_origin_base(repo)

        assert _local_conflicts(repo, "main") is False

    def test_a_branch_that_conflicts_with_the_base(self, tmp_path: Path) -> None:
        from issue_agent.runner.executor import _local_conflicts

        repo = _git_repo(tmp_path / "r")
        _checkout(repo, "-b", "work")
        _commit(repo, "alpha\nbeta-branch\ngamma\n", "branch edit")
        _checkout(repo, "main")
        _commit(repo, "alpha\nbeta-main\ngamma\n", "base edit")
        _set_origin_base(repo)
        _checkout(repo, "work")

        assert _local_conflicts(repo, "main") is True

    def test_unknown_when_git_cannot_tell(self, tmp_path: Path) -> None:
        from issue_agent.runner.executor import _local_conflicts

        repo = _git_repo(tmp_path / "r")  # no origin/main ref

        assert _local_conflicts(repo, "main") is None
        assert _local_conflicts(tmp_path / "missing", "main") is None

    def test_fetches_the_base_first_when_given_a_credential(self, tmp_path: Path) -> None:
        from issue_agent.runner.executor import _local_conflicts

        repo = _git_repo(tmp_path / "r")
        _set_origin_base(repo)
        with patch("issue_agent.runner.executor._run_git") as mock_git:
            _local_conflicts(repo, "main", {"GIT_CONFIG_COUNT": "1"})

        mock_git.assert_called_once_with(
            repo, "fetch", "origin", "main", env={"GIT_CONFIG_COUNT": "1"}
        )


class TestStillConflicting:
    def _out(self, pulls: MagicMock | None):
        from issue_agent.runner.executor import _Outbound

        plugin = FakeProviderWithPulls("fake", secret="s")
        out = _Outbound.for_task(plugin, _make_task())
        return (
            out
            if pulls is None
            else _Outbound(out.task, out.forge, pulls, out.transport, out.capabilities)
        )

    def test_the_forge_answer_wins(self) -> None:
        from issue_agent.runner.executor import _still_conflicting

        pulls = MagicMock(spec=PullRequestClient)
        pulls.pr_has_conflicts.return_value = True
        pr = _pr_ref(7, "stateway-io/stateway-agent-fixtures")

        with patch("issue_agent.runner.executor._local_conflicts") as local:
            assert _still_conflicting(self._out(pulls), pr, "sha", Path("."), "main", MagicMock())
        local.assert_not_called()

    @pytest.mark.parametrize("answer", [None, RuntimeError("down")])
    def test_falls_back_to_the_workspace_when_the_forge_cannot_say(self, answer) -> None:
        from issue_agent.runner.executor import _still_conflicting

        pulls = MagicMock(spec=PullRequestClient)
        if isinstance(answer, Exception):
            pulls.pr_has_conflicts.side_effect = answer
        else:
            pulls.pr_has_conflicts.return_value = answer
        pr = _pr_ref(7, "stateway-io/stateway-agent-fixtures")

        with patch("issue_agent.runner.executor._local_conflicts", return_value=True) as local:
            assert _still_conflicting(self._out(pulls), pr, "sha", Path("."), "main", MagicMock())
        assert local.call_args.args[:2] == (Path("."), "main")

    def test_without_a_pr_only_the_workspace_answers(self) -> None:
        from issue_agent.runner.executor import _Outbound, _still_conflicting

        out = _Outbound.for_task(FakeProvider("fake", secret="s"), _make_task())
        with patch("issue_agent.runner.executor._local_conflicts", return_value=None):
            assert _still_conflicting(out, None, "sha", Path("."), "main", MagicMock()) is False


class TestBranchPublishedComment:
    """The issue comment that stands in for a PR on a forge with no PR API."""

    _COMPARE = "https://forge.example/o/r/-/compare/main...agent/x"

    def _comment(self, **overrides) -> str:
        from issue_agent.runner.executor import _build_branch_published_comment

        fields = dict(
            task=_make_task(),
            branch="agent/x",
            compare_url=self._COMPARE,
            result=AgentResult(success=True, cost_usd=0.5, iterations=4),
            description="Fixed the link.",
            changed_files=[("README.md", "modified")],
            testing_notes="- [x] `pytest` passed",
            agent_name="Bot",
            pr_term="Merge Request",
        )
        fields.update(overrides)
        return _build_branch_published_comment(**fields)

    def test_first_delivery_links_the_compare_view_and_carries_the_summary(self) -> None:
        comment = self._comment()

        assert "pushed a proposed fix to branch `agent/x`" in comment
        assert f"[`main...agent/x`]({self._COMPARE})" in comment
        assert "No Merge Request was opened" in comment
        assert "## Summary\nFixed the link." in comment
        assert "- `README.md` — modified" in comment
        assert "`pytest` passed" in comment
        assert "Cost: US$ 0.50 | Iterations: 4" in comment
        assert "Closes" not in comment

    def test_follow_up_reports_the_base_merge_and_conflicts(self) -> None:
        comment = self._comment(
            updated_existing=True,
            base_update=BaseUpdate("merged"),
            still_conflicting=True,
        )

        assert "updated branch `agent/x` in response to the latest comment" in comment
        assert "Merged `main` into the branch." in comment
        assert "still reports conflicts between the branch and `main`" in comment

    def test_carried_over_work_says_so(self) -> None:
        comment = self._comment(carried_over_commits=2)

        assert "work an earlier run had already committed" in comment
        assert "2 commits ahead of `main`" in comment

    def test_initial_comment_on_a_published_branch_follow_up(self) -> None:
        work = IssueWork(
            issue_repo="stateway-io/stateway-issues",
            issue_number=42,
            target_repo="stateway-io/stateway-agent-fixtures",
            base_branch="main",
            work_branch="agent/x",
        )

        comment = _build_initial_comment(_make_task(), work, "Bot", published_branch=True)

        assert "resumed working on this issue to update its published branch" in comment
        assert "Work branch: `agent/x`" in comment


class TestReopenPublishedBranch:
    def test_a_branch_published_row_is_reopened_in_place(
        self, store: SQLiteStore, settings: Settings
    ) -> None:
        from issue_agent.runner.executor import _open_or_resume_issue_work

        task = _make_task()
        store.insert_task(task)
        store.open_issue_work(
            "github", task.repo, 42, task.target_repo, "main", "agent/issue-42-earlier-title"
        )
        store.update_issue_work(
            "github",
            task.repo,
            42,
            state="closed",
            closed_reason="branch_published",
            last_summary="earlier",
        )

        work, resumed, force_continue = _open_or_resume_issue_work(
            store,
            task,
            settings,
            settings.route_for(task.provider_instance, task.repo),
            "A new title",
        )

        assert (resumed, force_continue) == (True, True)
        assert work.state == "open"
        assert work.work_branch == "agent/issue-42-earlier-title"
        assert work.attempts == 0
        events = [e["type"] for e in _task_events(store, task.id)]
        assert "issue_work_reopened_from_branch" in events
