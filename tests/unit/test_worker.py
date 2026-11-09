from __future__ import annotations

import json
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.providers.registry import ProviderRegistry
from issue_agent.queue.models import Task, TaskStatus, TriggerKind
from issue_agent.queue.store import SQLiteStore
from issue_agent.worker import (
    _build_blocked_comment,
    _build_retry_exhausted_comment,
    _compute_backoff,
    _threshold_ts,
    run_orphan_recovery,
    run_retry_recovery,
    run_task,
)
from tests.fakes.fake_provider import FakeForge, FakeProvider
from tests.helpers.factories import make_author, make_comment, make_issue, make_trigger_event
from tests.helpers.settings import github_providers

_OLD_HEARTBEAT = "2020-01-01T00:00:00.000Z"


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


def _make_settings(**overrides) -> Settings:
    defaults = dict(
        PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
        LLM_API_KEY="k",
        DATA_DIR="/tmp/test-worker-data",
        REPO_ROUTES=[
            {
                "issues": "stateway-io/stateway-issues",
                "target": "stateway-io/stateway-agent-fixtures",
            }
        ],
    )
    defaults.update(overrides)
    return SettingsNoEnv(**defaults)


def _trigger_json(labels: list[str]) -> str:
    return make_trigger_event(
        issue=make_issue(number=42, labels=labels),
        actor=make_author(login="testuser"),
    ).model_dump_json()


def _make_task(**overrides: dict) -> Task:
    defaults = dict(
        delivery_id="delivery-1",
        repo="stateway-io/stateway-issues",
        issue_number=42,
        trigger_kind=TriggerKind.ISSUE_OPENED,
        trigger_json=_trigger_json([]),
        target_repo="stateway-io/stateway-agent-fixtures",
    )
    defaults.update(overrides)
    return Task(**defaults)


def _make_orphan(store: SQLiteStore) -> str:
    """Insert task, fetch (status=running), set stale heartbeat, return id."""
    task = _make_task()
    store.insert_task(task)
    store.fetch_next()
    store.conn.execute(
        "UPDATE tasks SET heartbeat_at = ? WHERE id = ?",
        (_OLD_HEARTBEAT, task.id),
    )
    store.conn.commit()
    return task.id


@pytest.fixture(autouse=True)
def _cleanup_audit() -> None:
    yield
    import shutil

    shutil.rmtree("/tmp/test-worker-data", ignore_errors=True)


@pytest.fixture
def store() -> SQLiteStore:
    s = SQLiteStore(":memory:")
    s.init()
    return s


@pytest.fixture
def settings() -> Settings:
    return _make_settings()


@pytest.fixture
def forge() -> FakeForge:
    return FakeForge()


@pytest.fixture
def registry(forge: FakeForge) -> ProviderRegistry:
    return ProviderRegistry({"github": FakeProvider("github", secret="s", forge=forge)})


class TestBuildBlockedComment:
    def test_uses_configured_agent_name(self) -> None:
        task = _make_task()
        comment = _build_blocked_comment(task, "some reason", "My Custom Bot")
        assert "My Custom Bot" in comment
        assert "Stateway" not in comment

    def test_includes_reason_and_task_id(self) -> None:
        task = _make_task()
        comment = _build_blocked_comment(task, "budget exceeded", "Agent X")
        assert "budget exceeded" in comment
        assert task.id in comment


class TestBuildRetryExhaustedComment:
    def test_uses_configured_agent_name(self) -> None:
        task = _make_task(retry_count=3, cost_usd=1.23, iterations=7)
        comment = _build_retry_exhausted_comment(task, "My Custom Bot")
        assert "My Custom Bot" in comment
        assert "Stateway" not in comment

    def test_includes_retry_count_and_cost(self) -> None:
        task = _make_task(retry_count=3, cost_usd=1.23, iterations=7)
        comment = _build_retry_exhausted_comment(task, "Agent X")
        assert "3 unsuccessful attempts" in comment
        assert "1.23" in comment

    def test_mentions_saved_branch_and_how_to_resume(self) -> None:
        task = _make_task(retry_count=3)
        comment = _build_retry_exhausted_comment(
            task, "Agent X", resumable_branch="agent/issue-42-fix"
        )
        assert "agent/issue-42-fix" in comment
        assert "comment" in comment.lower()

    def test_without_saved_branch_has_no_resume_hint(self) -> None:
        comment = _build_retry_exhausted_comment(_make_task(retry_count=3), "Agent X")
        assert "saved" not in comment.lower()


# ── Helper assertions ────────────────────────────────────────────


def _task_status(store: SQLiteStore, task_id: str) -> TaskStatus | None:
    t = store.get_task(task_id)
    return t.status if t else None


def _task_retry_count(store: SQLiteStore, task_id: str) -> int:
    t = store.get_task(task_id)
    return t.retry_count if t else -1


def _task_error(store: SQLiteStore, task_id: str) -> str | None:
    t = store.get_task(task_id)
    return t.error if t else None


def _task_events(store: SQLiteStore, task_id: str) -> list[dict]:
    return [
        {"type": e.event_type, "payload": json.loads(e.payload_json)}
        for e in store.get_task_events(task_id)
    ]


# ── _threshold_ts ────────────────────────────────────────────────


class TestThresholdTs:
    def test_returns_iso_string(self) -> None:
        result = _threshold_ts(90)
        assert result.endswith("Z")
        assert "T" in result

    def test_ago_is_before_now(self) -> None:
        future = _threshold_ts(-3600)
        past = _threshold_ts(3600)
        assert past < future


# ── Orphan Recovery ──────────────────────────────────────────────


class TestOrphanRecovery:
    def test_recovers_expired_task(self, store: SQLiteStore, settings: Settings) -> None:
        task_id = _make_orphan(store)

        count = run_orphan_recovery(store, settings)

        assert count == 1
        assert _task_status(store, task_id) is TaskStatus.PENDING
        assert _task_retry_count(store, task_id) == 1

    def test_recovery_clears_timestamps(self, store: SQLiteStore, settings: Settings) -> None:
        task_id = _make_orphan(store)

        run_orphan_recovery(store, settings)

        t = store.get_task(task_id)
        assert t is not None
        assert t.started_at is None
        assert t.heartbeat_at is None
        assert t.completed_at is None
        assert t.error is None

    def test_recovery_appends_task_event(self, store: SQLiteStore, settings: Settings) -> None:
        task_id = _make_orphan(store)

        run_orphan_recovery(store, settings)

        events = _task_events(store, task_id)
        recovery_events = [e for e in events if e["type"] == "orphaned_recovery"]
        assert len(recovery_events) == 1
        assert recovery_events[0]["payload"]["action"] == "reset_to_pending"

    def test_orphan_exhausted_retries(self, store: SQLiteStore, settings: Settings) -> None:
        task = _make_task()
        store.insert_task(task)
        store.fetch_next()
        store.update_status(task.id, TaskStatus.RUNNING, retry_count=3)
        store.conn.execute(
            "UPDATE tasks SET heartbeat_at = ? WHERE id = ?",
            (_OLD_HEARTBEAT, task.id),
        )
        store.conn.commit()

        count = run_orphan_recovery(store, settings)

        assert count == 0
        assert _task_status(store, task.id) is TaskStatus.FAILED_TERMINAL
        assert _task_error(store, task.id) == "exceeded_recovery_retries"

    def test_orphan_exhausted_on_boundary(self, store: SQLiteStore, settings: Settings) -> None:
        task = _make_task()
        store.insert_task(task)
        store.fetch_next()
        store.update_status(task.id, TaskStatus.RUNNING, retry_count=2)
        store.conn.execute(
            "UPDATE tasks SET heartbeat_at = ? WHERE id = ?",
            (_OLD_HEARTBEAT, task.id),
        )
        store.conn.commit()

        count = run_orphan_recovery(store, settings)

        assert count == 1
        assert _task_status(store, task.id) is TaskStatus.PENDING
        assert _task_retry_count(store, task.id) == 3

    def test_active_task_not_touched(self, store: SQLiteStore, settings: Settings) -> None:
        task = _make_task()
        store.insert_task(task)
        store.fetch_next()
        store.heartbeat(task.id)

        count = run_orphan_recovery(store, settings)

        assert count == 0
        assert _task_status(store, task.id) is TaskStatus.RUNNING

    def test_no_orphans_returns_zero(self, store: SQLiteStore, settings: Settings) -> None:
        assert run_orphan_recovery(store, settings) == 0

    def test_increments_retry_count(self, store: SQLiteStore, settings: Settings) -> None:
        task_id = _make_orphan(store)

        run_orphan_recovery(store, settings)
        assert _task_retry_count(store, task_id) == 1

        store.fetch_next()
        store.conn.execute(
            "UPDATE tasks SET heartbeat_at = ? WHERE id = ?",
            (_OLD_HEARTBEAT, task_id),
        )
        store.conn.commit()
        run_orphan_recovery(store, settings)
        assert _task_retry_count(store, task_id) == 2

    def test_cleans_workspace_dir(self, store: SQLiteStore, settings: Settings) -> None:
        task_id = _make_orphan(store)

        workspace = Path(settings.data_dir) / "workspaces" / task_id
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "some-file.txt").write_text("data")

        run_orphan_recovery(store, settings)
        assert not workspace.exists()

    def test_handles_workspace_cleanup_failure(
        self, store: SQLiteStore, settings: Settings
    ) -> None:
        task_id = _make_orphan(store)

        run_orphan_recovery(store, settings)
        assert _task_status(store, task_id) is TaskStatus.PENDING


# ── run_task ─────────────────────────────────────────────────────


class TestRunTask:
    @patch("issue_agent.worker._post_blocked_comment")
    def test_transitions_to_terminal(
        self,
        mock_post: MagicMock,
        store: SQLiteStore,
        settings: Settings,
        registry: ProviderRegistry,
    ) -> None:
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None

        run_task(store, fetched, settings, registry)

        status = _task_status(store, task.id)
        assert status is TaskStatus.ABORTED_POLICY
        assert store.get_task(task.id).completed_at is not None  # type: ignore[union-attr]

    @patch("issue_agent.worker._post_blocked_comment")
    def test_appends_state_change_event(
        self,
        mock_post: MagicMock,
        store: SQLiteStore,
        settings: Settings,
        registry: ProviderRegistry,
    ) -> None:
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None

        run_task(store, fetched, settings, registry)

        events = _task_events(store, task.id)
        state_changes = [e for e in events if e["type"] == "state_change"]
        assert len(state_changes) >= 1

    @patch("issue_agent.worker._post_blocked_comment")
    def test_handles_invalid_trigger(
        self,
        mock_post: MagicMock,
        store: SQLiteStore,
        settings: Settings,
        registry: ProviderRegistry,
    ) -> None:
        task = _make_task(trigger_json="not valid json at all {{{")
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None

        run_task(store, fetched, settings, registry)

        status = _task_status(store, task.id)
        assert status is TaskStatus.FAILED_TERMINAL
        assert "invalid_trigger" in (_task_error(store, task.id) or "")

    @patch("issue_agent.worker._post_blocked_comment")
    def test_handles_missing_trigger(
        self,
        mock_post: MagicMock,
        store: SQLiteStore,
        settings: Settings,
        registry: ProviderRegistry,
    ) -> None:
        task = _make_task(trigger_json=None)
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None

        run_task(store, fetched, settings, registry)

        assert _task_status(store, task.id) is TaskStatus.FAILED_TERMINAL
        assert "invalid_trigger" in (_task_error(store, task.id) or "")

    def test_exception_in_classify_is_caught(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry
    ) -> None:
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None

        with patch(
            "issue_agent.worker.classify_labels",
            side_effect=RuntimeError("boom"),
        ):
            run_task(store, fetched, settings, registry)

        assert _task_status(store, task.id) is TaskStatus.FAILED_TERMINAL
        assert _task_error(store, task.id) == "unhandled_worker_exception"

    @patch("issue_agent.worker._post_blocked_comment")
    def test_writes_audit_entry(
        self,
        mock_post: MagicMock,
        store: SQLiteStore,
        settings: Settings,
        registry: ProviderRegistry,
    ) -> None:
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None

        run_task(store, fetched, settings, registry)

        today = time.strftime("%Y-%m-%d", time.gmtime())
        audit_path = Path(settings.data_dir) / f"audit.{today}.log"
        assert audit_path.exists()
        lines = audit_path.read_text().strip().split("\n")
        assert any("task_processed" in line for line in lines)
        assert all('"provider_instance": "github"' in line for line in lines if "task_id" in line)


class TestDeferredCommentAuthorGuard:
    """A comment whose association the forge didn't report is judged here,
    before any spend, and dropped as silently as at ingestion (PRD §3.1)."""

    def _unknown_comment_task(self, login: str) -> Task:
        trigger = make_trigger_event(
            TriggerKind.COMMENT_COMMAND,
            issue=make_issue(number=42, labels=["bug"], author=make_author(login="reporter")),
            trigger_comment=make_comment(author=make_author(login=login, association="unknown")),
        )
        return _make_task(
            trigger_kind=TriggerKind.COMMENT_COMMAND,
            trigger_json=trigger.model_dump_json(),
        )

    @patch("issue_agent.worker.classify_labels")
    @patch("issue_agent.worker._post_blocked_comment")
    def test_rejected_author_aborts_silently(
        self,
        mock_post: MagicMock,
        mock_classify: MagicMock,
        store: SQLiteStore,
        settings: Settings,
        registry: ProviderRegistry,
    ) -> None:
        task = self._unknown_comment_task("stranger")
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None

        run_task(store, fetched, settings, registry)

        assert _task_status(store, task.id) is TaskStatus.ABORTED_POLICY
        assert _task_error(store, task.id) == "comment_author_not_allowed"
        mock_post.assert_not_called()
        mock_classify.assert_not_called()

    @patch("issue_agent.worker.classify_labels", side_effect=RuntimeError("reached"))
    def test_issue_author_proceeds_past_the_guard(
        self,
        mock_classify: MagicMock,
        store: SQLiteStore,
        settings: Settings,
        registry: ProviderRegistry,
    ) -> None:
        task = self._unknown_comment_task("reporter")
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None

        run_task(store, fetched, settings, registry)

        mock_classify.assert_called_once()


# ── main flow equivalent ─────────────────────────────────────────


class TestMainFlow:
    """Verifies that the sequence main() executes works
    (without testing the infinite loop directly)."""

    @patch("issue_agent.worker._post_blocked_comment")
    def test_processes_one_task(
        self,
        mock_post: MagicMock,
        store: SQLiteStore,
        settings: Settings,
        registry: ProviderRegistry,
    ) -> None:
        task = _make_task()
        store.insert_task(task)

        run_orphan_recovery(store, settings)
        fetched = store.fetch_next()
        assert fetched is not None
        run_task(store, fetched, settings, registry)

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.ABORTED_POLICY
        assert updated.completed_at is not None

    @patch("issue_agent.worker._post_blocked_comment")
    def test_orphan_recovery_then_process(
        self,
        mock_post: MagicMock,
        store: SQLiteStore,
        settings: Settings,
        registry: ProviderRegistry,
    ) -> None:
        task_id = _make_orphan(store)

        recovered = run_orphan_recovery(store, settings)
        assert recovered == 1

        fetched = store.fetch_next()
        assert fetched is not None
        assert fetched.id == task_id
        assert fetched.status is TaskStatus.RUNNING

        run_task(store, fetched, settings, registry)

        updated = store.get_task(task_id)
        assert updated is not None
        assert updated.status is TaskStatus.ABORTED_POLICY


# ── Signal handling ──────────────────────────────────────────────


class TestSignalHandler:
    def test_sigint_sets_running_false(self) -> None:
        import issue_agent.worker as w

        w._RUNNING = True
        w._signal_handler(signal.SIGINT, None)
        assert w._RUNNING is False

    def test_sigterm_sets_running_false(self) -> None:
        import issue_agent.worker as w

        w._RUNNING = True
        w._signal_handler(signal.SIGTERM, None)
        assert w._RUNNING is False


# ── Blocked Comment Posting (13.4.11) ─────────────────────────────


class TestBlockedCommentPosting:
    """Verifies that the 4 terminal gaps (payload, repo, author, skip)
    post a comment on the issue. The budget gap does NOT post a comment —
    it requeues the task instead of aborting it (see TestBudgetRequeue):
    posting "task aborted" on every poll cycle until the budget resets
    would spam the issue."""

    def _run(
        self, store: SQLiteStore, task: Task, settings: Settings, registry: ProviderRegistry
    ) -> None:
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None
        run_task(store, fetched, settings, registry)

    def test_payload_guard_posts_comment(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry, forge: FakeForge
    ) -> None:
        self._run(store, _make_task(trigger_json="not valid json at all {{{"), settings, registry)

        [body] = forge.posted("stateway-io/stateway-issues", 42)
        assert "webhook payload" in body.lower()

    def test_repo_guard_posts_comment(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry, forge: FakeForge
    ) -> None:
        self._run(store, _make_task(repo="some-other/repo"), settings, registry)

        [body] = forge.posted("some-other/repo", 42)
        assert "allowlist" in body.lower()

    def test_author_guard_posts_comment(
        self, store: SQLiteStore, registry: ProviderRegistry, forge: FakeForge
    ) -> None:
        s = _make_settings(ALLOWED_AUTHORS="someone-else")
        self._run(store, _make_task(), s, registry)

        [body] = forge.posted("stateway-io/stateway-issues", 42)
        assert "author" in body.lower()

    def test_skip_posts_comment(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry, forge: FakeForge
    ) -> None:
        self._run(store, _make_task(), settings, registry)

        [body] = forge.posted("stateway-io/stateway-issues", 42)
        assert "out of scope" in body.lower()

    def test_comment_failure_does_not_change_the_outcome(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry
    ) -> None:
        plugin = registry.get("github")
        failing = MagicMock()
        failing.post_comment.side_effect = RuntimeError("forge down")
        task = _make_task(repo="some-other/repo")
        with patch.object(plugin, "client", return_value=failing):
            self._run(store, task, settings, registry)

        assert _task_status(store, task.id) is TaskStatus.ABORTED_POLICY


# ── Comments go through the task's provider instance ──────────────


def _make_triage_task(**overrides: dict) -> Task:
    defaults = dict(
        label_category="question",
        trigger_json=_trigger_json(["question"]),
    )
    defaults.update(overrides)
    return _make_task(**defaults)


class TestCommentsGoThroughTheTasksProvider:
    """Every comment the worker posts goes through the plugin registered for
    ``task.provider_instance``, on the issue's own repo — never a client
    built from a per-task credential (PRD §3.2)."""

    def test_triage_comment_is_posted_on_the_issue_repo(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry, forge: FakeForge
    ) -> None:
        task = _make_triage_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None

        run_task(store, fetched, settings, registry)

        assert len(forge.posted("stateway-io/stateway-issues", 42)) == 1
        assert forge.posted("stateway-io/stateway-agent-fixtures", 42) == []
        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.SUCCEEDED

    def test_the_tasks_provider_instance_is_used(
        self, store: SQLiteStore, settings: Settings, forge: FakeForge
    ) -> None:
        other = FakeForge()
        registry = ProviderRegistry(
            {
                "github": FakeProvider("github", secret="s", forge=forge),
                "forge-b": FakeProvider("forge-b", secret="s", forge=other),
            }
        )
        task = _make_triage_task(provider_instance="forge-b")
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None

        run_task(store, fetched, settings, registry)

        assert len(other.posted("stateway-io/stateway-issues", 42)) == 1
        assert forge.posted("stateway-io/stateway-issues", 42) == []

    def test_unknown_provider_instance_fails_the_task(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry
    ) -> None:
        task = _make_triage_task(provider_instance="gone")
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None

        run_task(store, fetched, settings, registry)

        assert _task_status(store, task.id) is TaskStatus.FAILED_TERMINAL
        assert _task_error(store, task.id) == "unhandled_worker_exception"

    def test_deferred_comment_author_is_resolved_by_the_forge(
        self, store: SQLiteStore, settings: Settings
    ) -> None:
        forge = FakeForge(permissions={"maintainer": "member"})
        registry = ProviderRegistry({"github": FakeProvider("github", secret="s", forge=forge)})
        trigger = make_trigger_event(
            TriggerKind.COMMENT_COMMAND,
            issue=make_issue(number=42, labels=["question"]),
            trigger_comment=make_comment(
                author=make_author(login="maintainer", association="unknown")
            ),
        )
        task = _make_triage_task(
            trigger_kind=TriggerKind.COMMENT_COMMAND, trigger_json=trigger.model_dump_json()
        )
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None

        run_task(store, fetched, settings, registry)

        assert _task_status(store, task.id) is TaskStatus.SUCCEEDED


class TestDiagnoseDispatch:
    """LabelAction.DIAGNOSE delegates to runner.diagnose.run_diagnosis and
    never falls through to FIX (which would clone+edit+PR) or is silently
    dropped (leaving the task RUNNING forever — see TestExhaustiveDispatch)."""

    def _make_diagnose_task(self, **overrides) -> Task:
        defaults = dict(
            label_category="bug",
            trigger_json=_trigger_json(["bug"]),
        )
        defaults.update(overrides)
        return _make_task(**defaults)

    @patch("issue_agent.runner.diagnose.run_diagnosis")
    def test_diagnose_category_dispatches_to_run_diagnosis(
        self,
        mock_run_diagnosis: MagicMock,
        store: SQLiteStore,
        settings: Settings,
        registry: ProviderRegistry,
    ) -> None:
        settings = _make_settings(POLICY_DIAGNOSE_CATEGORIES=["bug"])
        task = self._make_diagnose_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None

        run_task(store, fetched, settings, registry)

        mock_run_diagnosis.assert_called_once()
        args, _ = mock_run_diagnosis.call_args
        assert args[1].id == fetched.id
        assert args[2] is settings
        assert args[3] is registry.get("github")

    @patch("issue_agent.runner.diagnose.run_diagnosis")
    @patch("issue_agent.runner.executor.run_task")
    def test_diagnose_category_never_reaches_fix(
        self,
        mock_executor_run_task: MagicMock,
        mock_run_diagnosis: MagicMock,
        store: SQLiteStore,
        settings: Settings,
        registry: ProviderRegistry,
    ) -> None:
        settings = _make_settings(POLICY_DIAGNOSE_CATEGORIES=["bug"])
        task = self._make_diagnose_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None

        run_task(store, fetched, settings, registry)

        mock_run_diagnosis.assert_called_once()
        mock_executor_run_task.assert_not_called()


class TestExhaustiveDispatch:
    """Every LabelAction member must have a branch in run_task's dispatch —
    an unhandled value used to leave the task RUNNING forever (no status
    update, picked up by orphan recovery only after orphan_timeout_seconds,
    looping until retries are exhausted). The dispatch now has an
    `else: raise AssertionError`, which the outer handler converts into a
    loud FAILED_TERMINAL/unhandled_worker_exception instead of a silent hang."""

    def test_all_label_actions_are_handled(self) -> None:
        import inspect

        from issue_agent.policy.label_router import LabelAction
        from issue_agent.worker import run_task

        source = inspect.getsource(run_task)
        for action in LabelAction:
            assert f"LabelAction.{action.name}" in source, (
                f"run_task has no dispatch branch for LabelAction.{action.name}"
            )


# ── Main loop budget check (13.5.1) ────────────────────────────────
#
# The proactive budget check that used to run in main()
# before fetch_next() is gone — there's no task in hand yet at that point,
# so no budget *scope* (task.target_repo) is known. The guard now lives
# entirely inside run_task, checked per-task against that task's own scope.


class TestMainLoopBudget:
    """Verifies that main() no longer checks the global budget before
    fetch_next() — even with the "global" budget blown in the store,
    fetch_next is always called (the real guard happens inside run_task,
    per task, scoped to task.target_repo)."""

    @patch("issue_agent.worker.time.sleep")
    @patch("issue_agent.worker.write_audit_entry")
    @patch("issue_agent.worker.SQLiteStore")
    @patch("issue_agent.worker.get_settings")
    @patch("issue_agent.worker.setup_logging")
    @patch("issue_agent.worker.prune_old_audit_files", return_value=[])
    @patch("issue_agent.worker.run_orphan_recovery", return_value=0)
    def test_calls_fetch_next_even_when_global_scope_budget_is_exceeded(
        self,
        mock_orphan: MagicMock,
        mock_prune: MagicMock,
        mock_setup: MagicMock,
        mock_get_settings: MagicMock,
        mock_store_cls: MagicMock,
        mock_audit: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        import issue_agent.worker as w

        mock_store = MagicMock(spec=SQLiteStore)
        mock_store_cls.return_value = mock_store

        s = _make_settings(MAX_DAILY_BUDGET_USD=10.0)
        mock_get_settings.return_value = s

        # Budget exceeded for the "global" scope — an older worker
        # would have skipped fetch_next() here without ever looking at a
        # task. Since the check moved into run_task (per task.target_repo),
        # this must no longer block the fetch.
        mock_budget = MagicMock()
        mock_budget.total_cost_usd = 999.0
        mock_budget.task_count = 20
        mock_store.get_daily_budget.return_value = mock_budget
        mock_store.fetch_next.return_value = None

        def _stop_worker(*args: object) -> None:
            w._RUNNING = False

        mock_sleep.side_effect = _stop_worker

        w._RUNNING = True
        w.main()

        mock_store.fetch_next.assert_called_once()
        mock_audit.assert_not_called()

    @patch("issue_agent.worker.time.sleep")
    @patch("issue_agent.worker.write_audit_entry")
    @patch("issue_agent.worker.SQLiteStore")
    @patch("issue_agent.worker.get_settings")
    @patch("issue_agent.worker.setup_logging")
    @patch("issue_agent.worker.prune_old_audit_files", return_value=[])
    @patch("issue_agent.worker.run_orphan_recovery", return_value=0)
    def test_calls_fetch_next_when_budget_ok(
        self,
        mock_orphan: MagicMock,
        mock_prune: MagicMock,
        mock_setup: MagicMock,
        mock_get_settings: MagicMock,
        mock_store_cls: MagicMock,
        mock_audit: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        import issue_agent.worker as w

        mock_store = MagicMock(spec=SQLiteStore)
        mock_store_cls.return_value = mock_store

        s = _make_settings(MAX_DAILY_BUDGET_USD=100.0)
        mock_get_settings.return_value = s

        # Budget OK
        mock_budget = MagicMock()
        mock_budget.total_cost_usd = 5.0
        mock_budget.task_count = 2
        mock_store.get_daily_budget.return_value = mock_budget

        mock_store.fetch_next.return_value = None

        def _stop_worker(*args: object) -> None:
            w._RUNNING = False

        mock_sleep.side_effect = _stop_worker

        w._RUNNING = True
        w.main()

        mock_store.fetch_next.assert_called_once()


class TestReconcileSweepInMain:
    """Main() runs the issue_work reconciliation sweep once at
    startup and then on its own interval timer."""

    @patch("issue_agent.worker.time.sleep")
    @patch("issue_agent.worker.reconcile_issue_work")
    @patch("issue_agent.worker.run_maintenance")
    @patch("issue_agent.worker.write_audit_entry")
    @patch("issue_agent.worker.SQLiteStore")
    @patch("issue_agent.worker.get_settings")
    @patch("issue_agent.worker.setup_logging")
    @patch("issue_agent.worker.prune_old_audit_files", return_value=[])
    @patch("issue_agent.worker.run_orphan_recovery", return_value=0)
    def test_reconcile_runs_at_startup(
        self,
        mock_orphan: MagicMock,
        mock_prune: MagicMock,
        mock_setup: MagicMock,
        mock_get_settings: MagicMock,
        mock_store_cls: MagicMock,
        mock_audit: MagicMock,
        mock_maint: MagicMock,
        mock_reconcile: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        import issue_agent.worker as w

        mock_store = MagicMock(spec=SQLiteStore)
        mock_store_cls.return_value = mock_store
        mock_get_settings.return_value = _make_settings()
        mock_store.fetch_next.return_value = None

        def _stop_worker(*args: object) -> None:
            w._RUNNING = False

        mock_sleep.side_effect = _stop_worker

        w._RUNNING = True
        w.main()

        mock_reconcile.assert_called_once_with(mock_store, mock_get_settings.return_value, ANY)
        registry = mock_reconcile.call_args.args[2]
        assert isinstance(registry, ProviderRegistry)
        assert registry.get("github").instance_id == "github"
        # Maintenance gets the same registry, for the PR-state sweep.
        mock_maint.assert_called_once_with(mock_store, mock_get_settings.return_value, registry)


class TestCIMonitorSweepInMain:
    """main() runs the CI monitor sweep on the first loop pass (picking up
    watches a restart interrupted) and then every poll interval."""

    @patch("issue_agent.worker.time.sleep")
    @patch("issue_agent.worker.run_ci_monitor")
    @patch("issue_agent.worker.reconcile_issue_work")
    @patch("issue_agent.worker.run_maintenance")
    @patch("issue_agent.worker.write_audit_entry")
    @patch("issue_agent.worker.SQLiteStore")
    @patch("issue_agent.worker.get_settings")
    @patch("issue_agent.worker.setup_logging")
    @patch("issue_agent.worker.prune_old_audit_files", return_value=[])
    @patch("issue_agent.worker.run_orphan_recovery", return_value=0)
    def test_sweeps_on_the_first_pass(
        self,
        mock_orphan: MagicMock,
        mock_prune: MagicMock,
        mock_setup: MagicMock,
        mock_get_settings: MagicMock,
        mock_store_cls: MagicMock,
        mock_audit: MagicMock,
        mock_maint: MagicMock,
        mock_reconcile: MagicMock,
        mock_ci: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        import issue_agent.worker as w

        mock_store = MagicMock(spec=SQLiteStore)
        mock_store_cls.return_value = mock_store
        mock_get_settings.return_value = _make_settings()
        mock_store.fetch_next.return_value = None

        def _stop_worker(*args: object) -> None:
            w._RUNNING = False

        mock_sleep.side_effect = _stop_worker

        w._RUNNING = True
        w.main()

        mock_ci.assert_called_once_with(mock_store, mock_get_settings.return_value, ANY)
        assert isinstance(mock_ci.call_args.args[2], ProviderRegistry)

    @patch("issue_agent.worker.time.monotonic", return_value=5.0)
    @patch("issue_agent.worker.time.sleep")
    @patch("issue_agent.worker.run_ci_monitor")
    @patch("issue_agent.worker.reconcile_issue_work")
    @patch("issue_agent.worker.run_maintenance")
    @patch("issue_agent.worker.write_audit_entry")
    @patch("issue_agent.worker.SQLiteStore")
    @patch("issue_agent.worker.get_settings")
    @patch("issue_agent.worker.setup_logging")
    @patch("issue_agent.worker.prune_old_audit_files", return_value=[])
    @patch("issue_agent.worker.run_orphan_recovery", return_value=0)
    def test_sweeps_on_the_first_pass_right_after_boot(
        self,
        mock_orphan: MagicMock,
        mock_prune: MagicMock,
        mock_setup: MagicMock,
        mock_get_settings: MagicMock,
        mock_store_cls: MagicMock,
        mock_audit: MagicMock,
        mock_maint: MagicMock,
        mock_reconcile: MagicMock,
        mock_ci: MagicMock,
        mock_sleep: MagicMock,
        mock_monotonic: MagicMock,
    ) -> None:
        # Regression: the monotonic clock counts from boot on Linux, so on a
        # freshly started machine it reads less than the poll interval.
        import issue_agent.worker as w

        mock_store = MagicMock(spec=SQLiteStore)
        mock_store_cls.return_value = mock_store
        mock_get_settings.return_value = _make_settings()
        mock_store.fetch_next.return_value = None

        def _stop_worker(*args: object) -> None:
            w._RUNNING = False

        mock_sleep.side_effect = _stop_worker

        w._RUNNING = True
        w.main()

        mock_ci.assert_called_once()


class TestBudgetAlertDedup:
    """run_task's budget-exceeded alert is deduped per
    (date, scope) via the module-level _budget_alerted dict — a repo stuck
    against its daily cap sends one alert, not one per blocked task. Each
    target_repo scope is tracked independently, so repo-b getting blocked
    still triggers its own alert even though repo-a already used up its
    one-per-day alert."""

    @patch("issue_agent.worker.time.sleep")
    @patch("issue_agent.worker.send_budget_alert")
    def test_alert_deduped_per_scope_but_independent_across_scopes(
        self,
        mock_alert: MagicMock,
        mock_sleep: MagicMock,
        store: SQLiteStore,
        registry: ProviderRegistry,
    ) -> None:
        import issue_agent.worker as w

        w._budget_alerted.clear()

        s = _make_settings(MAX_DAILY_BUDGET_USD=10.0)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        store.add_daily_budget(today, "owner/repo-a", 999.0)
        store.add_daily_budget(today, "owner/repo-b", 999.0)

        task_a1 = _make_task(target_repo="owner/repo-a", issue_number=101, delivery_id="d-a1")
        task_a2 = _make_task(target_repo="owner/repo-a", issue_number=102, delivery_id="d-a2")
        task_b1 = _make_task(target_repo="owner/repo-b", issue_number=103, delivery_id="d-b1")
        for t in (task_a1, task_a2, task_b1):
            store.insert_task(t)

        # A budget-blocked task is requeued to pending
        # (not aborted), so it's immediately eligible for fetch_next() again
        # — looping via fetch_next() here would just keep re-picking
        # whichever requeued task has the earliest created_at instead of
        # visiting all three. Drive run_task() directly off each inserted
        # Task instead; what's under test is the alert dedup, not the
        # queue's pick order.
        for t in (task_a1, task_a2, task_b1):
            run_task(store, t, s, registry)

        # 2 blocked scopes → 2 alerts total, not 3 (one per blocked task) —
        # a regression that dropped the dedup dict entirely would send 3;
        # a regression that deduped across all scopes (not just same-scope)
        # would send only 1.
        assert mock_alert.call_count == 2
        alerted_scopes = {key.rsplit(":", 1)[-1] for key in w._budget_alerted}
        assert alerted_scopes == {"owner/repo-a", "owner/repo-b"}


# ── Budget-exceeded requeues instead of aborting ──
#
# Originally, main()'s loop checked the (global) budget *before* calling
# fetch_next() at all, so a pending task just sat untouched in the queue
# until the budget reset. Per-repo budgets removed that proactive gate — correctly,
# since the budget's *scope* (task.target_repo) isn't known before a task
# is pulled — but that means fetch_next() now always hands run_task() a
# task, and the in-task guard used to respond by aborting it
# (ABORTED_BUDGET, a terminal status) instead of leaving it queued. That
# silently turned "automatically retried tomorrow" into "needs a manual
# re-trigger forever" for every issue registered after the daily cap hit.


class TestBudgetRequeue:
    @patch("issue_agent.worker.time.sleep")
    def test_budget_exceeded_requeues_to_pending_not_aborted(
        self,
        mock_sleep: MagicMock,
        store: SQLiteStore,
        registry: ProviderRegistry,
        forge: FakeForge,
    ) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        task = _make_task()
        store.add_daily_budget(today, task.target_repo, 999.0)
        s = _make_settings(MAX_DAILY_BUDGET_USD=10.0)
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None
        assert fetched.status is TaskStatus.RUNNING  # sanity: fetch_next() claimed it

        run_task(store, fetched, s, registry)

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.PENDING
        assert updated.status is not TaskStatus.ABORTED_BUDGET

    @patch("issue_agent.worker.time.sleep")
    def test_budget_exceeded_sleeps_one_poll_interval(
        self,
        mock_sleep: MagicMock,
        store: SQLiteStore,
        registry: ProviderRegistry,
        forge: FakeForge,
    ) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        task = _make_task()
        store.add_daily_budget(today, task.target_repo, 999.0)
        s = _make_settings(MAX_DAILY_BUDGET_USD=10.0, POLL_INTERVAL_SECONDS=7.5)
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None

        run_task(store, fetched, s, registry)

        mock_sleep.assert_called_once_with(7.5)

    @patch("issue_agent.worker.time.sleep")
    def test_budget_exceeded_does_not_post_abort_comment(
        self,
        mock_sleep: MagicMock,
        store: SQLiteStore,
        registry: ProviderRegistry,
        forge: FakeForge,
    ) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        task = _make_task()
        store.add_daily_budget(today, task.target_repo, 999.0)
        s = _make_settings(MAX_DAILY_BUDGET_USD=10.0)
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None

        run_task(store, fetched, s, registry)

        # The task will simply run again once the budget allows it — no
        # comment should be posted on the issue for a non-final outcome
        # (that would spam it once per poll cycle until the budget resets).
        assert forge.posted("stateway-io/stateway-issues", 42) == []

    @patch("issue_agent.worker.time.sleep")
    def test_budget_exceeded_preserves_retry_count(
        self,
        mock_sleep: MagicMock,
        store: SQLiteStore,
        registry: ProviderRegistry,
        forge: FakeForge,
    ) -> None:
        """Requeuing on a budget guard is not a task failure/retry — it
        must not consume/increment retry_count (which is reserved for
        actual transient failures, see run_retry_recovery). insert_task()
        always starts a task at retry_count=0 (it isn't one of the columns
        insert_task writes), so this asserts the guard leaves it at 0
        rather than bumping it the way orphan/retry recovery would."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        task = _make_task()
        store.add_daily_budget(today, task.target_repo, 999.0)
        s = _make_settings(MAX_DAILY_BUDGET_USD=10.0)
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None
        assert fetched.retry_count == 0  # sanity: insert_task() starts at 0

        run_task(store, fetched, s, registry)

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.retry_count == 0


# ── _compute_backoff ──────────────────────────────────────────────


_DEFAULT_SCHEDULE = [60, 300, 1800]


class TestComputeBackoff:
    def test_retry_count_zero_returns_60(self) -> None:
        assert _compute_backoff(0, _DEFAULT_SCHEDULE) == 60

    def test_retry_count_one_returns_300(self) -> None:
        assert _compute_backoff(1, _DEFAULT_SCHEDULE) == 300

    def test_retry_count_two_returns_1800(self) -> None:
        assert _compute_backoff(2, _DEFAULT_SCHEDULE) == 1800

    def test_retry_count_above_max_returns_max(self) -> None:
        assert _compute_backoff(3, _DEFAULT_SCHEDULE) == 1800
        assert _compute_backoff(10, _DEFAULT_SCHEDULE) == 1800

    def test_negative_is_treated_as_zero(self) -> None:
        assert _compute_backoff(-1, _DEFAULT_SCHEDULE) == 60

    def test_uses_custom_schedule(self) -> None:
        assert _compute_backoff(0, [10, 20]) == 10
        assert _compute_backoff(5, [10, 20]) == 20


# ── Retry Recovery ────────────────────────────────────────────────


_OLD_COMPLETED = "2020-01-01T00:00:00.000Z"


def _make_recoverable(
    store: SQLiteStore, completed_at: str | None = _OLD_COMPLETED, retry_count: int = 0
) -> str:
    task = _make_task()
    store.insert_task(task)
    store.update_status(
        task.id,
        TaskStatus.FAILED_RECOVERABLE,
        error="test_error",
        completed_at=completed_at,
        retry_count=retry_count,
        cost_usd=0.5,
        iterations=3,
    )
    return task.id


class TestRetryRecovery:
    def test_resets_with_old_completed(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry
    ) -> None:
        task_id = _make_recoverable(store, retry_count=0)
        count = run_retry_recovery(store, settings, registry)
        assert count == 1
        t = store.get_task(task_id)
        assert t is not None
        assert t.status is TaskStatus.PENDING
        assert t.retry_count == 1

    def test_resets_clears_timestamps(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry
    ) -> None:
        task_id = _make_recoverable(store, retry_count=0)
        run_retry_recovery(store, settings, registry)
        t = store.get_task(task_id)
        assert t is not None
        assert t.started_at is None
        assert t.heartbeat_at is None
        assert t.completed_at is None
        assert t.error is None

    def test_resets_appends_task_event(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry
    ) -> None:
        task_id = _make_recoverable(store, retry_count=0)
        run_retry_recovery(store, settings, registry)
        events = _task_events(store, task_id)
        recovery_events = [e for e in events if e["type"] == "retry_recovery"]
        assert len(recovery_events) == 1
        assert recovery_events[0]["payload"]["action"] == "reset_to_pending"
        assert recovery_events[0]["payload"]["retry_count"] == 1

    def test_skips_recent_completed(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry
    ) -> None:
        recent = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"
        task_id = _make_recoverable(store, completed_at=recent, retry_count=0)
        count = run_retry_recovery(store, settings, registry)
        assert count == 0
        t = store.get_task(task_id)
        assert t is not None
        assert t.status is TaskStatus.FAILED_RECOVERABLE

    def test_exhausted_becomes_terminal(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry
    ) -> None:
        task_id = _make_recoverable(store, retry_count=3)
        count = run_retry_recovery(store, settings, registry)
        assert count == 0
        t = store.get_task(task_id)
        assert t is not None
        assert t.status is TaskStatus.FAILED_TERMINAL
        assert t.error == "exhausted_retries"

    def test_exhausted_on_boundary(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry
    ) -> None:
        s = _make_settings(MAX_TASK_RETRIES=2)
        task_id = _make_recoverable(store, retry_count=2)
        count = run_retry_recovery(store, s, registry)
        assert count == 0
        t = store.get_task(task_id)
        assert t is not None
        assert t.status is TaskStatus.FAILED_TERMINAL

    def test_resets_on_boundary(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry
    ) -> None:
        task_id = _make_recoverable(store, retry_count=2)
        count = run_retry_recovery(store, settings, registry)
        assert count == 1
        t = store.get_task(task_id)
        assert t is not None
        assert t.status is TaskStatus.PENDING
        assert t.retry_count == 3

    def test_no_recoverable_tasks_returns_zero(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry
    ) -> None:
        task = _make_task()
        store.insert_task(task)
        store.fetch_next()
        count = run_retry_recovery(store, settings, registry)
        assert count == 0

    def test_no_tasks_at_all_returns_zero(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry
    ) -> None:
        assert run_retry_recovery(store, settings, registry) == 0

    def test_exhausted_appends_task_event(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry
    ) -> None:
        task_id = _make_recoverable(store, retry_count=3)
        run_retry_recovery(store, settings, registry)
        events = _task_events(store, task_id)
        exhausted_events = [e for e in events if e["type"] == "retry_exhausted"]
        assert len(exhausted_events) == 1
        assert exhausted_events[0]["payload"]["action"] == "failed_terminal"

    def test_exhausted_posts_comment(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry, forge: FakeForge
    ) -> None:
        _make_recoverable(store, retry_count=3)
        run_retry_recovery(store, settings, registry)
        [body] = forge.posted("stateway-io/stateway-issues", 42)
        assert "repeated failures" in body.lower()

    def test_exhausted_comment_points_to_saved_branch(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry, forge: FakeForge
    ) -> None:
        """A run that died on an LLM error left its WIP on the work branch and
        `issue_work` open: the give-up comment must say a comment resumes it."""
        store.open_issue_work(
            "github",
            "stateway-io/stateway-issues",
            42,
            "stateway-io/stateway-agent-fixtures",
            "main",
            "agent/issue-42-fix",
        )
        _make_recoverable(store, retry_count=3)
        run_retry_recovery(store, settings, registry)
        [body] = forge.posted("stateway-io/stateway-issues", 42)
        assert "agent/issue-42-fix" in body

    def test_exhausted_comment_omits_branch_when_work_was_closed(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry, forge: FakeForge
    ) -> None:
        _make_recoverable(store, retry_count=3)
        run_retry_recovery(store, settings, registry)
        [body] = forge.posted("stateway-io/stateway-issues", 42)
        assert "saved" not in body.lower()

    def test_reset_does_not_post_comment(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry, forge: FakeForge
    ) -> None:
        task_id = _make_recoverable(store, retry_count=0)
        run_retry_recovery(store, settings, registry)
        assert forge.posted("stateway-io/stateway-issues", 42) == []
        t = store.get_task(task_id)
        assert t is not None
        assert t.status is TaskStatus.PENDING

    def test_multiple_recoverable_tasks(
        self, store: SQLiteStore, settings: Settings, registry: ProviderRegistry
    ) -> None:
        id1 = _make_recoverable(store, retry_count=0)
        task2 = _make_task(issue_number=99)
        store.insert_task(task2)
        store.update_status(
            task2.id,
            TaskStatus.FAILED_RECOVERABLE,
            error="test_error",
            completed_at="2020-02-01T00:00:00.000Z",
            retry_count=1,
            cost_usd=1.0,
            iterations=5,
        )
        count = run_retry_recovery(store, settings, registry)
        assert count == 2
        t1 = store.get_task(id1)
        t2 = store.get_task(task2.id)
        assert t1 is not None and t1.status is TaskStatus.PENDING
        assert t2 is not None and t2.status is TaskStatus.PENDING
        assert t1.retry_count == 1
        assert t2.retry_count == 2
