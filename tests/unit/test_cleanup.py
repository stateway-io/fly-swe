from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.core.models import PRState
from issue_agent.observability.cleanup import (
    cleanup_database_records,
    cleanup_events,
    cleanup_orphan_workspaces,
    reconcile_issue_work,
    reconcile_pr_states,
    run_maintenance,
    select_for_retention,
    task_retention_policy,
)
from issue_agent.providers.registry import ProviderRegistry
from issue_agent.queue.models import Task, TaskStatus, TriggerKind
from issue_agent.queue.store import SQLiteStore
from tests.fakes.fake_provider import FakeForge, FakeProvider, FakeProviderWithPulls
from tests.helpers.settings import github_providers

if TYPE_CHECKING:
    from pathlib import Path


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


@pytest.fixture
def store() -> SQLiteStore:
    s = SQLiteStore(":memory:")
    s.init()
    return s


def _make_task(**overrides) -> Task:
    defaults = dict(
        delivery_id="del-1",
        repo="stateway-io/stateway-issues",
        issue_number=42,
        trigger_kind=TriggerKind.ISSUE_OPENED,
    )
    defaults.update(overrides)
    return Task(**defaults)


def _old_timestamp(days_ago: int) -> str:
    dt = datetime.now(UTC) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}"[:3] + "Z"


def _settings(data_dir: str, **overrides: object) -> Settings:
    fields: dict[str, object] = dict(
        PROVIDERS=github_providers(app_id=1, app_private_key="k", webhook_secret="s"),
        LLM_API_KEY="k",
        DATA_DIR=data_dir,
        EVENTS_RETENTION_DAYS=30,
        DB_RETENTION_DAYS=90,
        AUDIT_RETENTION_DAYS=90,
        CLEANUP_INTERVAL_MINUTES=60,
    )
    fields.update(overrides)
    return SettingsNoEnv(**fields)


# ── store methods ────────────────────────────────────────────


class TestFindCompletedTaskIdsOlderThan:
    def test_returns_old_completed_tasks(self, store: SQLiteStore) -> None:
        tid = store.insert_task(_make_task(issue_number=1))
        store.conn.execute(
            "UPDATE tasks SET status=?, completed_at=? WHERE id=?",
            (TaskStatus.SUCCEEDED.value, _old_timestamp(60), tid),
        )
        store.conn.commit()

        ids = store.find_completed_task_ids_older_than(_old_timestamp(30))
        assert tid in ids

    def test_excludes_recent_tasks(self, store: SQLiteStore) -> None:
        tid = store.insert_task(_make_task(issue_number=2))
        store.conn.execute(
            "UPDATE tasks SET status=?, completed_at=? WHERE id=?",
            (TaskStatus.SUCCEEDED.value, _old_timestamp(10), tid),
        )
        store.conn.commit()

        ids = store.find_completed_task_ids_older_than(_old_timestamp(30))
        assert tid not in ids

    def test_excludes_running_tasks(self, store: SQLiteStore) -> None:
        tid = store.insert_task(_make_task(issue_number=3))
        store.update_status(tid, TaskStatus.RUNNING)

        ids = store.find_completed_task_ids_older_than(_old_timestamp(1))
        assert tid not in ids

    def test_excludes_tasks_without_completed_at(self, store: SQLiteStore) -> None:
        tid = store.insert_task(_make_task(issue_number=4))
        ids = store.find_completed_task_ids_older_than(_old_timestamp(1))
        assert tid not in ids


class TestFindRunningTaskIds:
    def test_returns_running_ids(self, store: SQLiteStore) -> None:
        t1 = store.insert_task(_make_task(issue_number=1))
        t2 = store.insert_task(_make_task(issue_number=2, delivery_id="del-2"))
        store.update_status(t1, TaskStatus.RUNNING)

        running = store.find_running_task_ids()
        assert t1 in running
        assert t2 not in running

    def test_empty_when_none_running(self, store: SQLiteStore) -> None:
        store.insert_task(_make_task())
        assert store.find_running_task_ids() == set()


class TestDeleteTasks:
    def test_deletes_task_events_and_tasks(self, store: SQLiteStore) -> None:
        tid = store.insert_task(_make_task(issue_number=1))
        keep = store.insert_task(_make_task(issue_number=2, delivery_id="del-2"))
        store.append_task_event(tid, "test", {"msg": "old"})
        store.append_task_event(keep, "test", {"msg": "keep"})

        assert store.delete_tasks([tid]) == {"task_events": 1, "tasks": 1}
        assert store.get_task(tid) is None
        assert store.get_task(keep) is not None
        assert len(store.get_task_events(keep)) == 1

    def test_empty_list_is_a_noop(self, store: SQLiteStore) -> None:
        assert store.delete_tasks([]) == {"task_events": 0, "tasks": 0}

    def test_more_ids_than_one_chunk(self, store: SQLiteStore) -> None:
        ids = [
            store.insert_task(_make_task(issue_number=n, delivery_id=f"d{n}"))
            for n in range(1, 1202)
        ]
        assert store.delete_tasks(ids)["tasks"] == 1201

    def test_rolls_back_on_error(self, store: SQLiteStore) -> None:
        tid = store.insert_task(_make_task(issue_number=1))
        store.append_task_event(tid, "test", {})
        store.conn.execute(
            "CREATE TRIGGER no_task_delete BEFORE DELETE ON tasks "
            "BEGIN SELECT RAISE(ABORT, 'nope'); END"
        )
        with pytest.raises(Exception, match="nope"):
            store.delete_tasks([tid])
        assert len(store.get_task_events(tid)) == 1


class TestPruneDeliveriesAndBudget:
    def test_prunes_old_deliveries_and_budget(self, store: SQLiteStore) -> None:
        store.mark_delivery_processed("github", "old-del", "issues", "opened")
        store.mark_delivery_processed("github", "new-del", "issues", "opened")
        store.conn.execute(
            "UPDATE processed_deliveries SET received_at=? WHERE delivery_id='old-del'",
            (_old_timestamp(100),),
        )
        store.add_daily_budget(_old_timestamp(100)[:10], "global", 1.0)
        store.add_daily_budget(_old_timestamp(1)[:10], "global", 1.0)
        store.conn.commit()

        counts = store.prune_deliveries_and_budget(_old_timestamp(30))
        assert counts == {"processed_deliveries": 1, "daily_budget": 1}
        assert not store.is_delivery_processed("github", "old-del")
        assert store.is_delivery_processed("github", "new-del")


# ── cleanup functions ────────────────────────────────────────


class TestCleanupEvents:
    def test_removes_event_dirs_for_task_ids(self, tmp_path: Path) -> None:
        events_dir = tmp_path / "events"
        (events_dir / "task-a").mkdir(parents=True)
        (events_dir / "task-b").mkdir(parents=True)
        (events_dir / "task-c").mkdir(parents=True)

        count = cleanup_events(str(tmp_path), ["task-a", "task-c"])
        assert count == 2
        assert not (events_dir / "task-a").exists()
        assert (events_dir / "task-b").exists()
        assert not (events_dir / "task-c").exists()

    def test_ignores_missing_task_dirs(self, tmp_path: Path) -> None:
        events_dir = tmp_path / "events"
        (events_dir / "existing").mkdir(parents=True)

        count = cleanup_events(str(tmp_path), ["existing", "missing"])
        assert count == 1

    def test_no_events_dir(self, tmp_path: Path) -> None:
        count = cleanup_events(str(tmp_path), ["task-a"])
        assert count == 0


class TestCleanupOrphanWorkspaces:
    def test_removes_orphan_workspaces(self, tmp_path: Path) -> None:
        ws_dir = tmp_path / "workspaces"
        (ws_dir / "orphan-1").mkdir(parents=True)
        (ws_dir / "orphan-2").mkdir(parents=True)
        (ws_dir / "active-1").mkdir(parents=True)

        count = cleanup_orphan_workspaces(str(tmp_path), {"active-1"})
        assert count == 2
        assert not (ws_dir / "orphan-1").exists()
        assert not (ws_dir / "orphan-2").exists()
        assert (ws_dir / "active-1").exists()

    def test_removes_nothing_when_all_active(self, tmp_path: Path) -> None:
        ws_dir = tmp_path / "workspaces"
        (ws_dir / "active-1").mkdir(parents=True)

        count = cleanup_orphan_workspaces(str(tmp_path), {"active-1"})
        assert count == 0
        assert (ws_dir / "active-1").exists()

    def test_no_workspaces_dir(self, tmp_path: Path) -> None:
        count = cleanup_orphan_workspaces(str(tmp_path), set())
        assert count == 0

    def test_skips_files_not_dirs(self, tmp_path: Path) -> None:
        ws_dir = tmp_path / "workspaces"
        ws_dir.mkdir(parents=True)
        (ws_dir / "not-a-dir").write_text("file")

        count = cleanup_orphan_workspaces(str(tmp_path), set())
        assert count == 0
        assert (ws_dir / "not-a-dir").exists()


def _finish(store: SQLiteStore, tid: str, status: TaskStatus, days_ago: int, **extra) -> None:
    store.update_status(tid, status, **extra)
    store.conn.execute(
        "UPDATE tasks SET completed_at=? WHERE id=?", (_old_timestamp(days_ago), tid)
    )
    store.conn.commit()


def _policy(**overrides) -> dict[str, int | None]:
    policy: dict[str, int | None] = {
        "pr_merged": 0,
        "pr_closed": 0,
        "aborted": 90,
        "failed": 90,
        "other": 90,
    }
    policy.update(overrides)
    return policy


ISSUES = "stateway-io/stateway-issues"
CODE = "stateway-io/code"


class TestSelectForRetention:
    """The per-category policy, on the store's rows."""

    def _pr_issue(
        self,
        store: SQLiteStore,
        issue: int,
        pr: int,
        state: str | None,
        closed_days_ago: int = 1,
    ) -> list[str]:
        """An issue worked into PR ``pr`` (triage + fix), the PR in ``state``."""
        triage = store.insert_task(_make_task(issue_number=issue, delivery_id=f"t{issue}"))
        _finish(store, triage, TaskStatus.SUCCEEDED, 200)
        fix = store.insert_task(
            _make_task(
                issue_number=issue,
                delivery_id=f"f{issue}",
                trigger_kind=TriggerKind.LABEL_ADDED,
                target_repo=CODE,
            )
        )
        _finish(store, fix, TaskStatus.SUCCEEDED, 200, pr_number=pr)
        store.open_issue_work("github", ISSUES, issue, CODE, "main", f"agent/issue-{issue}")
        store.close_issue_work("github", ISSUES, issue, "pr_opened", pr_number=pr)
        if state is not None:
            store.upsert_pr_state("github", CODE, pr, state, _old_timestamp(closed_days_ago))
        return [triage, fix]

    def test_merged_pr_deletes_the_whole_issue_history(self, store: SQLiteStore) -> None:
        ids = self._pr_issue(store, 1, 10, "merged")
        sel = select_for_retention(store, _policy())
        assert sorted(sel.task_ids["pr_merged"]) == sorted(ids)
        assert sel.issue_work_keys == [("github", ISSUES, 1)]

    def test_closed_pr_has_its_own_category(self, store: SQLiteStore) -> None:
        ids = self._pr_issue(store, 1, 10, "closed")
        sel = select_for_retention(store, _policy())
        assert sorted(sel.task_ids["pr_closed"]) == sorted(ids)
        assert sel.task_ids["pr_merged"] == []

    def test_pr_clock_starts_at_the_close_not_the_task(self, store: SQLiteStore) -> None:
        # Tasks finished 200 days ago, PR merged 5 days ago, 30-day retention.
        self._pr_issue(store, 1, 10, "merged", closed_days_ago=5)
        assert select_for_retention(store, _policy(pr_merged=30)).all_task_ids == []
        assert len(select_for_retention(store, _policy(pr_merged=3)).all_task_ids) == 2

    def test_forever_keeps_pr_issues(self, store: SQLiteStore) -> None:
        self._pr_issue(store, 1, 10, "merged")
        sel = select_for_retention(store, _policy(pr_merged=None))
        assert sel.all_task_ids == []
        assert sel.issue_work_keys == []

    def test_forever_on_merged_is_not_overridden_by_age_rules(self, store: SQLiteStore) -> None:
        # The triage task is 200 days old; "other" (90d) must not take it.
        self._pr_issue(store, 1, 10, "merged")
        assert select_for_retention(store, _policy(pr_merged=None, other=0)).all_task_ids == []

    @pytest.mark.parametrize("state", ["open", None])
    def test_open_or_unchecked_pr_keeps_everything(
        self, store: SQLiteStore, state: str | None
    ) -> None:
        self._pr_issue(store, 1, 10, state)
        sel = select_for_retention(store, _policy(other=0))
        assert sel.all_task_ids == []
        assert sel.issue_work_keys == []

    def test_any_open_pr_keeps_an_issue_with_an_earlier_closed_one(
        self, store: SQLiteStore
    ) -> None:
        self._pr_issue(store, 1, 10, "closed")
        later = store.insert_task(_make_task(issue_number=1, delivery_id="later", target_repo=CODE))
        _finish(store, later, TaskStatus.SUCCEEDED, 1, pr_number=11)
        store.upsert_pr_state("github", CODE, 11, "open", None)
        assert select_for_retention(store, _policy()).all_task_ids == []

    def test_merged_wins_over_closed_and_latest_close_is_the_clock(
        self, store: SQLiteStore
    ) -> None:
        self._pr_issue(store, 1, 10, "closed", closed_days_ago=50)
        later = store.insert_task(_make_task(issue_number=1, delivery_id="later", target_repo=CODE))
        _finish(store, later, TaskStatus.SUCCEEDED, 40, pr_number=11)
        store.upsert_pr_state("github", CODE, 11, "merged", _old_timestamp(10))

        assert select_for_retention(store, _policy(pr_merged=20)).all_task_ids == []
        sel = select_for_retention(store, _policy(pr_merged=5, pr_closed=None))
        assert len(sel.task_ids["pr_merged"]) == 3

    def test_pending_task_keeps_the_issue(self, store: SQLiteStore) -> None:
        self._pr_issue(store, 1, 10, "merged")
        store.insert_task(_make_task(issue_number=1, delivery_id="pending"))
        sel = select_for_retention(store, _policy())
        assert sel.all_task_ids == []
        assert sel.issue_work_keys == []

    def test_open_issue_work_keeps_the_issue(self, store: SQLiteStore) -> None:
        self._pr_issue(store, 1, 10, "merged")
        store.update_issue_work("github", ISSUES, 1, state="open", closed_reason=None)
        assert select_for_retention(store, _policy()).all_task_ids == []

    def test_issue_work_only_issue_is_selected(self, store: SQLiteStore) -> None:
        # Its tasks were already pruned by the old rule; the row lingers.
        store.open_issue_work("github", ISSUES, 5, CODE, "main", "agent/issue-5")
        store.close_issue_work("github", ISSUES, 5, "pr_opened", pr_number=50)
        store.upsert_pr_state("github", CODE, 50, "merged", _old_timestamp(1))
        assert select_for_retention(store, _policy()).issue_work_keys == [("github", ISSUES, 5)]

    def test_pr_without_forge_timestamp_uses_when_it_was_seen(self, store: SQLiteStore) -> None:
        self._pr_issue(store, 1, 10, None)
        store.upsert_pr_state("github", CODE, 10, "merged", None)
        assert len(select_for_retention(store, _policy()).task_ids["pr_merged"]) == 2
        assert select_for_retention(store, _policy(pr_merged=1)).all_task_ids == []

    @pytest.mark.parametrize(
        ("status", "category"),
        [
            (TaskStatus.ABORTED_BUDGET, "aborted"),
            (TaskStatus.ABORTED_POLICY, "aborted"),
            (TaskStatus.FAILED_TERMINAL, "failed"),
            (TaskStatus.SUCCEEDED, "other"),
        ],
    )
    def test_age_rules_by_status(
        self, store: SQLiteStore, status: TaskStatus, category: str
    ) -> None:
        old = store.insert_task(_make_task(issue_number=1, delivery_id="old"))
        _finish(store, old, status, 100)
        recent = store.insert_task(_make_task(issue_number=2, delivery_id="recent"))
        _finish(store, recent, status, 10)

        sel = select_for_retention(store, _policy())
        assert sel.task_ids[category] == [old]
        assert sel.issue_work_keys == []
        assert select_for_retention(store, _policy(**{category: None})).all_task_ids == []
        assert len(select_for_retention(store, _policy(**{category: 0})).all_task_ids) == 2

    @pytest.mark.parametrize(
        "status", [TaskStatus.FAILED_RECOVERABLE, TaskStatus.RUNNING, TaskStatus.PENDING]
    )
    def test_never_selects_tasks_that_may_still_run(
        self, store: SQLiteStore, status: TaskStatus
    ) -> None:
        tid = store.insert_task(_make_task(issue_number=1))
        if status is not TaskStatus.PENDING:
            _finish(store, tid, status, 400)
        sel = select_for_retention(store, _policy(other=0, failed=0, aborted=0))
        assert sel.all_task_ids == []

    def test_failed_recoverable_keeps_its_issue(self, store: SQLiteStore) -> None:
        old = store.insert_task(_make_task(issue_number=1, delivery_id="old"))
        _finish(store, old, TaskStatus.ABORTED_POLICY, 400)
        retry = store.insert_task(_make_task(issue_number=1, delivery_id="retry"))
        _finish(store, retry, TaskStatus.FAILED_RECOVERABLE, 1)
        assert select_for_retention(store, _policy(aborted=0)).all_task_ids == []


class TestCleanupDatabaseRecords:
    def test_deletes_selected_rows_and_reports_per_category(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
        settings = _settings(str(tmp_path))
        aborted = store.insert_task(_make_task(issue_number=1, delivery_id="a"))
        _finish(store, aborted, TaskStatus.ABORTED_BUDGET, 100)
        store.append_task_event(aborted, "test", {})
        fix = store.insert_task(_make_task(issue_number=2, delivery_id="f", target_repo=CODE))
        _finish(store, fix, TaskStatus.SUCCEEDED, 1, pr_number=7)
        store.open_issue_work("github", ISSUES, 2, CODE, "main", "agent/issue-2")
        store.close_issue_work("github", ISSUES, 2, "pr_opened", pr_number=7)
        store.upsert_pr_state("github", CODE, 7, "merged", _old_timestamp(1))
        store.upsert_pr_state("github", CODE, 99, "merged", None)  # orphan

        result, deleted = cleanup_database_records(store, settings)

        assert sorted(deleted) == sorted([aborted, fix])
        assert result.db_tasks == 2
        assert result.db_task_events == 1
        assert result.db_tasks_by_category["aborted"] == 1
        assert result.db_tasks_by_category["pr_merged"] == 1
        assert result.db_issue_work == 1
        assert store.get_issue_work("github", ISSUES, 2) is None
        # Both PR-state rows lost their last reference.
        assert result.db_pr_states == 2

    def test_task_rows_are_not_tied_to_events_days(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
        # Past events_days (trajectory gone) but within its category: the row stays.
        tid = store.insert_task(_make_task(issue_number=4))
        _finish(store, tid, TaskStatus.SUCCEEDED, 45)
        result, _ = cleanup_database_records(store, _settings(str(tmp_path)))
        assert result.db_tasks == 0
        assert store.get_task(tid) is not None

    def test_prunes_deliveries_by_db_days_without_any_task(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
        store.mark_delivery_processed("github", "old-del", "issues", "opened")
        store.conn.execute("UPDATE processed_deliveries SET received_at=?", (_old_timestamp(100),))
        store.conn.commit()
        result, _ = cleanup_database_records(store, _settings(str(tmp_path)))
        assert result.db_deliveries == 1

    def test_nothing_to_do_returns_zeros(self, store: SQLiteStore, tmp_path: Path) -> None:
        result, deleted = cleanup_database_records(store, _settings(str(tmp_path)))
        assert deleted == []
        assert result.total == 0


class TestTaskRetentionPolicy:
    def test_reads_settings(self, tmp_path: Path) -> None:
        settings = _settings(str(tmp_path), RETENTION_TASKS_FAILED="forever")
        assert task_retention_policy(settings) == {
            "pr_merged": 0,
            "pr_closed": 0,
            "aborted": 90,
            "failed": None,
            "other": 90,
        }


class TestReconcilePRStates:
    def _task_with_pr(self, store: SQLiteStore, pr: int, instance: str = "github") -> None:
        tid = store.insert_task(
            _make_task(
                issue_number=pr,
                delivery_id=f"d{pr}",
                target_repo=CODE,
                provider_instance=instance,
            )
        )
        _finish(store, tid, TaskStatus.SUCCEEDED, 1, pr_number=pr)

    def test_records_merged_closed_and_open(self, store: SQLiteStore, tmp_path: Path) -> None:
        for pr in (1, 2, 3):
            self._task_with_pr(store, pr)
        merged_at = datetime(2026, 10, 1, 12, 0, 0, 123456, tzinfo=UTC)
        states = {
            1: PRState(state="merged", closed_at=merged_at),
            2: PRState(state="closed", closed_at=merged_at),
            3: PRState(state="open"),
        }
        checked = reconcile_pr_states(
            store,
            _settings(str(tmp_path)),
            ProviderRegistry({}),
            pr_state_fn=lambda _i, _r, n: states[n],
        )
        assert checked == 3
        row = store.get_pr_state("github", CODE, 1)
        assert row is not None
        assert (row["state"], row["closed_at"]) == ("merged", "2026-10-01T12:00:00.123Z")
        assert store.get_pr_state("github", CODE, 3)["state"] == "open"  # type: ignore[index]
        # Final states are never looked up again; open ones are.
        assert store.list_prs_to_check(10) == [("github", CODE, 3)]

    def test_both_pr_rules_forever_makes_no_lookup(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
        self._task_with_pr(store, 1)
        settings = _settings(
            str(tmp_path), RETENTION_TASKS_PR_MERGED="forever", RETENTION_TASKS_PR_CLOSED=None
        )
        calls: list[int] = []
        checked = reconcile_pr_states(
            store,
            settings,
            ProviderRegistry({}),
            pr_state_fn=lambda _i, _r, n: calls.append(n) or PRState(state="open"),
        )
        assert checked == 0
        assert calls == []

    def test_respects_the_per_cycle_cap(self, store: SQLiteStore, tmp_path: Path) -> None:
        for pr in (1, 2, 3):
            self._task_with_pr(store, pr)
        settings = _settings(str(tmp_path), RETENTION_PR_CHECK_MAX_PER_CYCLE=2)
        checked = reconcile_pr_states(
            store, settings, ProviderRegistry({}), pr_state_fn=lambda *_: PRState(state="open")
        )
        assert checked == 2

    def test_one_row_error_does_not_abort_the_sweep(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
        for pr in (1, 2):
            self._task_with_pr(store, pr)

        def _state(_i: str, _r: str, n: int) -> PRState:
            if n == 1:
                raise RuntimeError("boom")
            return PRState(state="merged", closed_at=datetime.now(UTC))

        checked = reconcile_pr_states(
            store, _settings(str(tmp_path)), ProviderRegistry({}), pr_state_fn=_state
        )
        assert checked == 1
        assert store.get_pr_state("github", CODE, 1) is None

    def test_setup_failure_is_swallowed(self, store: SQLiteStore, tmp_path: Path) -> None:
        store.conn.execute("DROP TABLE pr_state")
        assert reconcile_pr_states(store, _settings(str(tmp_path)), ProviderRegistry({})) == 0

    def test_nothing_to_check_is_a_noop(self, store: SQLiteStore, tmp_path: Path) -> None:
        assert reconcile_pr_states(store, _settings(str(tmp_path)), ProviderRegistry({})) == 0

    def test_default_probe_asks_the_recording_instance(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
        plugin = FakeProviderWithPulls("fake", secret="s")
        plugin.forge.pr_states[(CODE, 1)] = PRState(
            state="merged", closed_at=datetime(2026, 10, 1, tzinfo=UTC)
        )
        self._task_with_pr(store, 1, instance="fake")
        self._task_with_pr(store, 2, instance="gone")  # no longer configured

        checked = reconcile_pr_states(
            store, _settings(str(tmp_path)), ProviderRegistry({"fake": plugin})
        )
        assert checked == 1
        assert store.get_pr_state("fake", CODE, 1)["state"] == "merged"  # type: ignore[index]
        assert store.get_pr_state("gone", CODE, 2) is None

    def test_forge_without_a_pr_api_fails_only_its_row(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
        self._task_with_pr(store, 1, instance="fake")
        checked = reconcile_pr_states(
            store,
            _settings(str(tmp_path)),
            ProviderRegistry({"fake": FakeProvider("fake", secret="s")}),
        )
        assert checked == 0


# ── run_maintenance (integration-style) ──────────────────────


class TestRunMaintenance:
    def test_removes_old_events_and_workspaces(self, store: SQLiteStore, tmp_path: Path) -> None:
        settings = _settings(str(tmp_path))

        old_tid = store.insert_task(_make_task(issue_number=1))
        store.conn.execute(
            "UPDATE tasks SET status=?, completed_at=? WHERE id=?",
            (TaskStatus.SUCCEEDED.value, _old_timestamp(60), old_tid),
        )

        recent_tid = store.insert_task(_make_task(issue_number=2, delivery_id="del-2"))
        store.conn.execute(
            "UPDATE tasks SET status=?, completed_at=? WHERE id=?",
            (TaskStatus.SUCCEEDED.value, _old_timestamp(5), recent_tid),
        )

        active_tid = store.insert_task(_make_task(issue_number=3, delivery_id="del-3"))
        store.update_status(active_tid, TaskStatus.RUNNING)
        store.conn.commit()

        (tmp_path / "events" / old_tid).mkdir(parents=True)
        (tmp_path / "events" / recent_tid).mkdir(parents=True)
        (tmp_path / "workspaces" / "orphan-ws").mkdir(parents=True)
        (tmp_path / "workspaces" / active_tid).mkdir(parents=True)

        result = run_maintenance(store, settings)

        assert result.events_removed == 1
        assert not (tmp_path / "events" / old_tid).exists()
        assert (tmp_path / "events" / recent_tid).exists()
        assert result.workspaces_removed == 1
        assert not (tmp_path / "workspaces" / "orphan-ws").exists()
        assert (tmp_path / "workspaces" / active_tid).exists()

    def test_empty_data_dir_does_not_raise(self, store: SQLiteStore, tmp_path: Path) -> None:
        settings = _settings(str(tmp_path))
        result = run_maintenance(store, settings)
        assert result.total == 0

    def test_prunes_audit_files_by_configured_retention(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
        # audit_days must actually govern the cut: a 45-day-old file survives
        # the function's default (90) and is pruned only because the config
        # says 30 — which proves run_maintenance passes the configured value.
        settings = SettingsNoEnv(
            PROVIDERS=github_providers(app_id=1, app_private_key="k", webhook_secret="s"),
            LLM_API_KEY="k",
            DATA_DIR=str(tmp_path),
            AUDIT_RETENTION_DAYS=30,
        )

        old_date = (datetime.now(UTC) - timedelta(days=45)).strftime("%Y-%m-%d")
        recent_date = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%d")
        (tmp_path / f"audit.{old_date}.log").write_text('{"event":"x"}\n')
        (tmp_path / f"audit.{recent_date}.log").write_text('{"event":"y"}\n')

        result = run_maintenance(store, settings)

        assert result.audit_files_pruned == 1
        assert not (tmp_path / f"audit.{old_date}.log").exists()
        assert (tmp_path / f"audit.{old_date}.log.gz").exists()
        assert (tmp_path / f"audit.{recent_date}.log").exists()


class TestRunMaintenanceRetention:
    """The PR sweep → per-category deletion → trajectory cleanup chain."""

    def _merged_issue(self, store: SQLiteStore, plugin: FakeProviderWithPulls) -> str:
        tid = store.insert_task(
            _make_task(
                issue_number=1,
                delivery_id="fix",
                target_repo=CODE,
                provider_instance="github",
            )
        )
        _finish(store, tid, TaskStatus.SUCCEEDED, 2, pr_number=10)
        store.open_issue_work("github", ISSUES, 1, CODE, "main", "agent/issue-1")
        store.close_issue_work("github", ISSUES, 1, "pr_opened", pr_number=10)
        plugin.forge.pr_states[(CODE, 10)] = PRState(
            state="merged", closed_at=datetime.now(UTC) - timedelta(hours=1)
        )
        return tid

    def test_merged_pr_is_pruned_on_the_next_run(self, store: SQLiteStore, tmp_path: Path) -> None:
        plugin = FakeProviderWithPulls("github", secret="s")
        tid = self._merged_issue(store, plugin)
        (tmp_path / "events" / tid).mkdir(parents=True)

        result = run_maintenance(
            store, _settings(str(tmp_path)), ProviderRegistry({"github": plugin})
        )

        assert result.prs_checked == 1
        assert result.db_tasks_by_category["pr_merged"] == 1
        assert result.db_issue_work == 1
        assert result.db_pr_states == 1
        assert store.get_task(tid) is None
        assert store.get_issue_work("github", ISSUES, 1) is None
        # The trajectory goes with the row even though it's inside events_days.
        assert not (tmp_path / "events" / tid).exists()
        assert result.events_removed == 1

        audit = [
            json.loads(line)
            for f in tmp_path.glob("audit.*.log")
            for line in f.read_text().splitlines()
        ]
        entry = next(e for e in audit if e["event"] == "maintenance_cleanup")
        assert entry["db_tasks_by_category"]["pr_merged"] == 1
        assert entry["task_retention"] == {
            "pr_merged": 0,
            "pr_closed": 0,
            "aborted": 90,
            "failed": 90,
            "other": 90,
        }

    def test_forever_is_written_as_text_in_the_audit(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
        old = store.insert_task(_make_task(issue_number=2, delivery_id="old"))
        _finish(store, old, TaskStatus.ABORTED_POLICY, 100)
        run_maintenance(store, _settings(str(tmp_path), RETENTION_TASKS_PR_MERGED="forever"))
        audit = [
            json.loads(line)
            for f in tmp_path.glob("audit.*.log")
            for line in f.read_text().splitlines()
        ]
        entry = next(e for e in audit if e["event"] == "maintenance_cleanup")
        assert entry["task_retention"]["pr_merged"] == "forever"

    def test_without_a_registry_no_pr_is_looked_up(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
        plugin = FakeProviderWithPulls("github", secret="s")
        tid = self._merged_issue(store, plugin)
        result = run_maintenance(store, _settings(str(tmp_path)))
        assert result.prs_checked == 0
        assert store.get_task(tid) is not None  # PR state still unknown

    def test_a_failure_is_contained(self, store: SQLiteStore, tmp_path: Path) -> None:
        store.conn.execute("DROP TABLE pr_state")
        result = run_maintenance(store, _settings(str(tmp_path)))
        assert result.total == 0


def _registry(forge: FakeForge | None = None) -> ProviderRegistry:
    return ProviderRegistry({"github": FakeProvider("github", secret="s", forge=forge)})


class TestReconcileIssueWork:
    """The sweep that closes an open ``issue_work`` whose
    issue was closed behind the agent's back (missed webhook / downtime)."""

    def _open(self, store: SQLiteStore, n: int) -> None:
        store.open_issue_work("github", "owner/issues", n, "owner/code", "main", f"agent/issue-{n}")

    def test_closes_work_whose_issue_is_closed(self, store: SQLiteStore, tmp_path: Path) -> None:
        settings = _settings(str(tmp_path))
        self._open(store, 1)

        result = reconcile_issue_work(
            store, settings, _registry(), issue_state_fn=lambda instance, repo, num: "closed"
        )

        assert result.closed == 1
        assert result.checked == 1
        work = store.get_issue_work("github", "owner/issues", 1)
        assert work.state == "closed"
        assert work.closed_reason == "issue_closed"

    def test_default_probe_asks_the_default_provider_instance(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
        settings = _settings(str(tmp_path))
        self._open(store, 1)
        self._open(store, 2)
        forge = FakeForge(issue_states={("owner/issues", 1): "closed"})

        result = reconcile_issue_work(store, settings, _registry(forge))

        assert result.checked == 2
        assert result.closed == 1
        assert store.get_issue_work("github", "owner/issues", 1).state == "closed"
        assert store.get_issue_work("github", "owner/issues", 2).state == "open"

    def test_each_row_is_looked_up_on_its_own_instance(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
        settings = _settings(str(tmp_path))
        store.open_issue_work("github", "owner/issues", 1, "owner/code", "main", "a")
        store.open_issue_work("ghe", "owner/issues", 1, "owner/code", "main", "b")
        registry = ProviderRegistry(
            {
                "github": FakeProvider("github", secret="s"),
                "ghe": FakeProvider(
                    "ghe",
                    secret="s",
                    forge=FakeForge(issue_states={("owner/issues", 1): "closed"}),
                ),
            }
        )

        result = reconcile_issue_work(store, settings, registry)

        assert result.closed == 1
        assert store.get_issue_work("github", "owner/issues", 1).state == "open"
        assert store.get_issue_work("ghe", "owner/issues", 1).state == "closed"

    def test_unknown_instance_fails_only_its_row(self, store: SQLiteStore, tmp_path: Path) -> None:
        settings = _settings(str(tmp_path))
        store.open_issue_work("gone", "owner/issues", 1, "owner/code", "main", "a")
        store.open_issue_work("github", "owner/issues", 2, "owner/code", "main", "b")
        forge = FakeForge(issue_states={("owner/issues", 2): "closed"})

        result = reconcile_issue_work(store, settings, _registry(forge))

        assert result.closed == 1
        assert store.get_issue_work("gone", "owner/issues", 1).state == "open"

    def test_leaves_work_whose_issue_is_still_open(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
        settings = _settings(str(tmp_path))
        self._open(store, 1)

        result = reconcile_issue_work(
            store, settings, _registry(), issue_state_fn=lambda instance, repo, num: "open"
        )

        assert result.closed == 0
        assert store.get_issue_work("github", "owner/issues", 1).state == "open"

    def test_respects_the_per_cycle_cap(self, store: SQLiteStore, tmp_path: Path) -> None:
        settings = _settings(str(tmp_path))
        settings = settings.model_copy(update={"resume_reconcile_max_per_cycle": 2})
        for n in range(5):
            self._open(store, n)
            time.sleep(0.005)
        seen: list[int] = []

        def _state(instance: str, repo: str, num: int) -> str:
            seen.append(num)
            return "open"

        reconcile_issue_work(store, settings, _registry(), issue_state_fn=_state)

        assert seen == [0, 1]

    def test_one_row_error_does_not_abort_the_sweep(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
        settings = _settings(str(tmp_path))
        self._open(store, 1)
        time.sleep(0.005)
        self._open(store, 2)

        def _state(instance: str, repo: str, num: int) -> str:
            if num == 1:
                raise RuntimeError("GitHub 500")
            return "closed"

        result = reconcile_issue_work(store, settings, _registry(), issue_state_fn=_state)

        assert result.closed == 1
        assert store.get_issue_work("github", "owner/issues", 1).state == "open"
        assert store.get_issue_work("github", "owner/issues", 2).state == "closed"

    def test_close_writes_an_audit_entry(self, store: SQLiteStore, tmp_path: Path) -> None:
        settings = _settings(str(tmp_path))
        self._open(store, 1)
        store.update_issue_work("github", "owner/issues", 1, attempts=2)

        reconcile_issue_work(
            store, settings, _registry(), issue_state_fn=lambda instance, repo, num: "closed"
        )

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        entries = [
            json.loads(line)
            for line in (tmp_path / f"audit.{today}.log").read_text().strip().split("\n")
        ]
        reconciled = next(e for e in entries if e["event"] == "issue_work_reconciled")
        assert reconciled["issue_number"] == 1
        assert reconciled["attempts"] == 2
        assert reconciled["provider_instance"] == "github"

    def test_no_open_work_is_a_noop(self, store: SQLiteStore, tmp_path: Path) -> None:
        settings = _settings(str(tmp_path))
        called = False

        def _state(instance: str, repo: str, num: int) -> str:
            nonlocal called
            called = True
            return "open"

        result = reconcile_issue_work(store, settings, _registry(), issue_state_fn=_state)

        assert result.checked == 0
        assert result.closed == 0
        assert called is False
