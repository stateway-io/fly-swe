from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.observability.cleanup import (
    cleanup_database_records,
    cleanup_events,
    cleanup_orphan_workspaces,
    reconcile_issue_work,
    run_maintenance,
)
from issue_agent.providers.registry import ProviderRegistry
from issue_agent.queue.models import Task, TaskStatus, TriggerKind
from issue_agent.queue.store import SQLiteStore
from tests.fakes.fake_provider import FakeForge, FakeProvider
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
        payload_json=json.dumps({"action": "opened", "issue": {"number": 42}}),
    )
    defaults.update(overrides)
    return Task(**defaults)


def _old_timestamp(days_ago: int) -> str:
    dt = datetime.now(UTC) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}"[:3] + "Z"


def _settings(data_dir: str) -> Settings:
    return SettingsNoEnv(
        PROVIDERS=github_providers(app_id=1, app_private_key="k", webhook_secret="s"),
        LLM_API_KEY="k",
        DATA_DIR=data_dir,
        EVENTS_RETENTION_DAYS=30,
        DB_RETENTION_DAYS=90,
        AUDIT_RETENTION_DAYS=90,
        CLEANUP_INTERVAL_MINUTES=60,
    )


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
        t2 = store.insert_task(_make_task(
            issue_number=2, delivery_id="del-2"
        ))
        store.update_status(t1, TaskStatus.RUNNING)

        running = store.find_running_task_ids()
        assert t1 in running
        assert t2 not in running

    def test_empty_when_none_running(self, store: SQLiteStore) -> None:
        store.insert_task(_make_task())
        assert store.find_running_task_ids() == set()


class TestDeleteOldRecords:
    def test_deletes_task_events_and_tasks(self, store: SQLiteStore) -> None:
        tid = store.insert_task(_make_task(issue_number=1))
        store.conn.execute(
            "UPDATE tasks SET status=?, completed_at=? WHERE id=?",
            (TaskStatus.SUCCEEDED.value, _old_timestamp(100), tid),
        )
        store.append_task_event(tid, "test", {"msg": "old"})
        store.conn.commit()

        counts = store.delete_old_records([tid], _old_timestamp(60))
        assert counts["tasks"] == 1
        assert counts["task_events"] == 1
        assert store.get_task(tid) is None

    def test_preserves_recent_records(self, store: SQLiteStore) -> None:
        tid = store.insert_task(_make_task(issue_number=2))

        counts = store.delete_old_records([tid], _old_timestamp(30))
        assert counts["tasks"] == 1
        assert store.get_task(tid) is None

    def test_skips_empty_list(self, store: SQLiteStore) -> None:
        counts = store.delete_old_records([], _old_timestamp(30))
        assert counts == {}


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


class TestCleanupDatabaseRecords:
    def test_deletes_old_records(self, store: SQLiteStore) -> None:
        tid = store.insert_task(_make_task(issue_number=1))
        store.conn.execute(
            "UPDATE tasks SET status=?, completed_at=? WHERE id=?",
            (TaskStatus.SUCCEEDED.value, _old_timestamp(60), tid),
        )
        store.append_task_event(tid, "test", {"msg": "old"})
        store.conn.commit()

        counts = cleanup_database_records(store, events_days=30, db_days=30)
        assert counts["tasks"] == 1
        assert counts["task_events"] >= 1
        assert store.get_task(tid) is None

    def test_preserves_recent_tasks(self, store: SQLiteStore) -> None:
        tid = store.insert_task(_make_task(issue_number=2))
        store.conn.execute(
            "UPDATE tasks SET status=?, completed_at=? WHERE id=?",
            (TaskStatus.SUCCEEDED.value, _old_timestamp(5), tid),
        )
        store.conn.commit()

        counts = cleanup_database_records(store, events_days=30, db_days=30)
        assert counts["tasks"] == 0
        assert store.get_task(tid) is not None

    def test_skips_running_tasks(self, store: SQLiteStore) -> None:
        tid = store.insert_task(_make_task(issue_number=3))
        store.update_status(tid, TaskStatus.RUNNING)
        store.conn.commit()

        counts = cleanup_database_records(store, events_days=1, db_days=1)
        assert counts["tasks"] == 0

    def test_no_old_tasks_returns_zeros(self, store: SQLiteStore) -> None:
        counts = cleanup_database_records(store, events_days=1, db_days=1)
        for v in counts.values():
            assert v == 0


# ── run_maintenance (integration-style) ──────────────────────


class TestRunMaintenance:
    def test_removes_old_events_and_workspaces(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
        settings = _settings(str(tmp_path))

        old_tid = store.insert_task(_make_task(issue_number=1))
        store.conn.execute(
            "UPDATE tasks SET status=?, completed_at=? WHERE id=?",
            (TaskStatus.SUCCEEDED.value, _old_timestamp(60), old_tid),
        )

        recent_tid = store.insert_task(_make_task(
            issue_number=2, delivery_id="del-2"
        ))
        store.conn.execute(
            "UPDATE tasks SET status=?, completed_at=? WHERE id=?",
            (TaskStatus.SUCCEEDED.value, _old_timestamp(5), recent_tid),
        )

        active_tid = store.insert_task(_make_task(
            issue_number=3, delivery_id="del-3"
        ))
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

    def test_empty_data_dir_does_not_raise(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
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


def _registry(forge: FakeForge | None = None) -> ProviderRegistry:
    return ProviderRegistry({"github": FakeProvider("github", secret="s", forge=forge)})


class TestReconcileIssueWork:
    """R7.3–R7.5: the sweep that closes an open ``issue_work`` whose
    issue was closed behind the agent's back (missed webhook / downtime)."""

    def _open(self, store: SQLiteStore, n: int) -> None:
        store.open_issue_work(
            "github",
            "owner/issues", n, "owner/code", "main", f"agent/issue-{n}"
        )

    def test_closes_work_whose_issue_is_closed(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
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

    def test_unknown_instance_fails_only_its_row(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
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

    def test_respects_the_per_cycle_cap(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
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

    def test_close_writes_an_audit_entry(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
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

    def test_no_open_work_is_a_noop(
        self, store: SQLiteStore, tmp_path: Path
    ) -> None:
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
