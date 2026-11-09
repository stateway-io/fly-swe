from __future__ import annotations

import json
import sqlite3
import time

import pytest

from issue_agent.queue.models import IssueWork, Task, TaskStatus, TriggerKind
from issue_agent.queue.store import SQLiteStore
from tests.helpers.factories import make_task, make_trigger_event


@pytest.fixture
def store() -> SQLiteStore:
    s = SQLiteStore(":memory:")
    s.init()
    return s


def _make_task(**overrides: dict) -> Task:
    defaults = dict(
        delivery_id="delivery-1",
        repo="stateway-io/stateway-issues",
        issue_number=42,
        trigger_kind=TriggerKind.ISSUE_OPENED,
    )
    defaults.update(overrides)
    return Task(**defaults)


class TestInit:
    def test_tables_created(self, store: SQLiteStore) -> None:
        tables = store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = [r["name"] for r in tables]
        assert "processed_deliveries" in names
        assert "tasks" in names
        assert "task_events" in names
        assert "daily_budget" in names

    def test_init_is_idempotent(self, store: SQLiteStore) -> None:
        store.init()
        store.init()

    def test_wal_mode(self, store: SQLiteStore) -> None:
        row = store.conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0].lower() in ("wal", "memory")

    def test_foreign_keys_on(self, store: SQLiteStore) -> None:
        row = store.conn.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1


class TestInsertAndFetch:
    def test_insert_task_assigns_id(self, store: SQLiteStore) -> None:
        task = _make_task()
        task_id = store.insert_task(task)
        assert len(task_id) == 32
        assert task.id == task_id

    def test_insert_task_sets_created_at(self, store: SQLiteStore) -> None:
        task = _make_task()
        store.insert_task(task)
        assert task.created_at != ""

    def test_fetch_next_returns_pending_task(self, store: SQLiteStore) -> None:
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None
        assert fetched.id == task.id
        assert fetched.status is TaskStatus.RUNNING

    def test_fetch_next_returns_none_when_empty(self, store: SQLiteStore) -> None:
        assert store.fetch_next() is None

    def test_fetch_next_does_not_return_non_pending(self, store: SQLiteStore) -> None:
        task = _make_task()
        store.insert_task(task)
        store.fetch_next()
        assert store.fetch_next() is None

    def test_fetch_next_respects_order(self, store: SQLiteStore) -> None:
        t1 = _make_task(delivery_id="d1", issue_number=1)
        t2 = _make_task(delivery_id="d2", issue_number=2)
        store.insert_task(t1)
        store.insert_task(t2)
        first = store.fetch_next()
        assert first is not None
        assert first.id == t1.id

    def test_fetch_next_sets_timestamps(self, store: SQLiteStore) -> None:
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()
        assert fetched is not None
        assert fetched.started_at is not None
        assert fetched.heartbeat_at is not None
        assert fetched.started_at == fetched.heartbeat_at

    def test_get_task_by_id(self, store: SQLiteStore) -> None:
        task = _make_task()
        store.insert_task(task)
        retrieved = store.get_task(task.id)
        assert retrieved is not None
        assert retrieved.id == task.id


class TestUpdateStatus:
    def test_update_to_running(self, store: SQLiteStore) -> None:
        task = _make_task()
        store.insert_task(task)
        store.update_status(task.id, TaskStatus.RUNNING)
        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.RUNNING
        assert updated.started_at is not None
        assert updated.heartbeat_at is not None

    def test_update_to_terminal_sets_completed_at(self, store: SQLiteStore) -> None:
        task = _make_task()
        store.insert_task(task)
        store.update_status(task.id, TaskStatus.SUCCEEDED)
        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status is TaskStatus.SUCCEEDED
        assert updated.completed_at is not None

    def test_update_with_extra_fields(self, store: SQLiteStore) -> None:
        task = _make_task()
        store.insert_task(task)
        store.update_status(
            task.id,
            TaskStatus.FAILED_TERMINAL,
            error="something went wrong",
            pr_number=99,
            cost_usd=0.42,
            iterations=5,
        )
        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.error == "something went wrong"
        assert updated.pr_number == 99
        assert updated.cost_usd == 0.42
        assert updated.iterations == 5
        assert updated.status is TaskStatus.FAILED_TERMINAL

    def test_all_terminal_statuses_set_completed_at(self, store: SQLiteStore) -> None:
        statuses = [
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED_RECOVERABLE,
            TaskStatus.FAILED_TERMINAL,
            TaskStatus.ABORTED_BUDGET,
            TaskStatus.ABORTED_POLICY,
        ]
        for status in statuses:
            t = _make_task(delivery_id=f"d-{status.value}")
            store.insert_task(t)
            store.update_status(t.id, status)
            retrieved = store.get_task(t.id)
            assert retrieved is not None
            assert retrieved.completed_at is not None, f"{status} should set completed_at"

    def test_update_nonexistent_task_does_not_raise(self, store: SQLiteStore) -> None:
        store.update_status("nonexistent", TaskStatus.SUCCEEDED)


class TestHeartbeat:
    def test_heartbeat_updates_timestamp(self, store: SQLiteStore) -> None:
        task = _make_task()
        store.insert_task(task)
        store.update_status(task.id, TaskStatus.RUNNING)
        first = store.get_task(task.id)
        assert first is not None
        first_heartbeat = first.heartbeat_at
        time.sleep(0.01)
        store.heartbeat(task.id)
        second = store.get_task(task.id)
        assert second is not None
        assert second.heartbeat_at != first_heartbeat
        assert second.heartbeat_at > first_heartbeat  # type: ignore[operator]


class TestQueueDepth:
    def test_depth_counts_only_pending(self, store: SQLiteStore) -> None:
        t1 = _make_task(delivery_id="d1", issue_number=1)
        t2 = _make_task(delivery_id="d2", issue_number=2)
        t3 = _make_task(delivery_id="d3", issue_number=3)
        store.insert_task(t1)
        store.insert_task(t2)
        store.insert_task(t3)
        assert store.get_queue_depth() == 3
        store.fetch_next()
        assert store.get_queue_depth() == 2

    def test_depth_zero_when_empty(self, store: SQLiteStore) -> None:
        assert store.get_queue_depth() == 0


class TestActiveCreationTaskCheck:
    def test_pending_opened_task_is_active(self, store: SQLiteStore) -> None:
        task = _make_task(delivery_id="d1", issue_number=7)
        store.insert_task(task)
        assert (
            store.has_active_creation_task_for_issue("github", "stateway-io/stateway-issues", 7)
            is True
        )

    def test_pending_labeled_task_is_active(self, store: SQLiteStore) -> None:
        task = _make_task(delivery_id="d1", issue_number=7, trigger_kind=TriggerKind.LABEL_ADDED)
        store.insert_task(task)
        assert (
            store.has_active_creation_task_for_issue("github", "stateway-io/stateway-issues", 7)
            is True
        )

    def test_running_task_is_active(self, store: SQLiteStore) -> None:
        task = _make_task(delivery_id="d1", issue_number=7)
        store.insert_task(task)
        store.fetch_next()
        assert (
            store.has_active_creation_task_for_issue("github", "stateway-io/stateway-issues", 7)
            is True
        )

    def test_completed_task_is_not_active(self, store: SQLiteStore) -> None:
        from issue_agent.queue.models import TaskStatus

        task = _make_task(delivery_id="d1", issue_number=7)
        store.insert_task(task)
        store.update_status(task.id, TaskStatus.ABORTED_BUDGET)
        assert (
            store.has_active_creation_task_for_issue("github", "stateway-io/stateway-issues", 7)
            is False
        )

    def test_different_issue_is_not_active(self, store: SQLiteStore) -> None:
        task = _make_task(delivery_id="d1", issue_number=7)
        store.insert_task(task)
        assert (
            store.has_active_creation_task_for_issue("github", "stateway-io/stateway-issues", 8)
            is False
        )

    def test_empty_store_is_not_active(self, store: SQLiteStore) -> None:
        assert (
            store.has_active_creation_task_for_issue("github", "stateway-io/stateway-issues", 7)
            is False
        )

    def test_comment_command_task_is_not_a_creation_task(self, store: SQLiteStore) -> None:
        """An in-flight comment_command task must never count here — it
        would make a genuinely new, unrelated `issues.labeled` event get
        silently dropped just because some other task is active on the
        same issue."""
        task = _make_task(
            delivery_id="d1", issue_number=7, trigger_kind=TriggerKind.COMMENT_COMMAND
        )
        store.insert_task(task)
        assert (
            store.has_active_creation_task_for_issue("github", "stateway-io/stateway-issues", 7)
            is False
        )


class TestInsertTaskUnlessActiveCreationTask:
    def test_inserts_when_no_active_creation_task(self, store: SQLiteStore) -> None:
        task = _make_task(delivery_id="d1", issue_number=20)
        task_id = store.insert_task_unless_active_creation_task(task)
        assert task_id is not None
        assert store.get_task(task_id) is not None

    def test_skips_when_opened_already_active(self, store: SQLiteStore) -> None:
        first = _make_task(delivery_id="d1", issue_number=21)
        store.insert_task(first)

        second = _make_task(delivery_id="d2", issue_number=21, trigger_kind=TriggerKind.LABEL_ADDED)
        task_id = store.insert_task_unless_active_creation_task(second)

        assert task_id is None
        assert store.get_queue_depth() == 1

    def test_skips_when_labeled_already_active(self, store: SQLiteStore) -> None:
        first = _make_task(delivery_id="d1", issue_number=22, trigger_kind=TriggerKind.LABEL_ADDED)
        store.insert_task(first)

        second = _make_task(delivery_id="d2", issue_number=22)
        task_id = store.insert_task_unless_active_creation_task(second)

        assert task_id is None
        assert store.get_queue_depth() == 1

    def test_ignores_unrelated_comment_command_task(self, store: SQLiteStore) -> None:
        """A comment_command task in flight for the issue must not block a
        genuinely new creation-trigger task (Fix 2's scoping)."""
        comment_task = _make_task(
            delivery_id="d1", issue_number=23, trigger_kind=TriggerKind.COMMENT_COMMAND
        )
        store.insert_task(comment_task)

        labeled_task = _make_task(
            delivery_id="d2", issue_number=23, trigger_kind=TriggerKind.LABEL_ADDED
        )
        task_id = store.insert_task_unless_active_creation_task(labeled_task)

        assert task_id is not None
        assert store.get_queue_depth() == 2


class TestOrphanedTasks:
    def test_find_orphaned(self, store: SQLiteStore) -> None:
        task = _make_task()
        store.insert_task(task)
        store.fetch_next()
        orphans = store.find_orphaned_tasks("1970-01-01T00:00:00Z")
        assert len(orphans) == 0

        orphans = store.find_orphaned_tasks("2099-01-01T00:00:00Z")
        assert len(orphans) == 1
        assert orphans[0].id == task.id

    def test_orphaned_excludes_succeeded(self, store: SQLiteStore) -> None:
        task = _make_task()
        store.insert_task(task)
        store.update_status(task.id, TaskStatus.SUCCEEDED)
        orphans = store.find_orphaned_tasks("1970-01-01T00:00:00Z")
        assert len(orphans) == 0


class TestTaskEvents:
    def test_append_and_retrieve(self, store: SQLiteStore) -> None:
        task = _make_task()
        store.insert_task(task)
        store.append_task_event(task.id, "state_change", {"from": "pending", "to": "running"})
        store.append_task_event(task.id, "llm_call", {"tokens": 123})
        events = store.get_task_events(task.id)
        assert len(events) == 2
        assert events[0].event_type == "state_change"
        assert events[1].event_type == "llm_call"

    def test_empty_task_events(self, store: SQLiteStore) -> None:
        task = _make_task()
        store.insert_task(task)
        assert store.get_task_events(task.id) == []

    def test_event_payload_is_stored(self, store: SQLiteStore) -> None:
        task = _make_task()
        store.insert_task(task)
        store.append_task_event(task.id, "test", {"key": "value", "nested": {"a": 1}})
        events = store.get_task_events(task.id)
        payload = json.loads(events[0].payload_json)
        assert payload["key"] == "value"
        assert payload["nested"]["a"] == 1


class TestRepoFieldsRoundtrip:
    """target_repo/base_branch/workflow ."""

    def test_insert_and_get_task_roundtrip(self, store: SQLiteStore) -> None:
        task = _make_task(
            target_repo="owner/target",
            base_branch="dev",
            workflow="gitflow",
        )
        store.insert_task(task)

        retrieved = store.get_task(task.id)
        assert retrieved is not None
        assert retrieved.target_repo == "owner/target"
        assert retrieved.base_branch == "dev"
        assert retrieved.workflow == "gitflow"

    def test_fetch_next_roundtrip(self, store: SQLiteStore) -> None:
        task = _make_task(
            target_repo="owner/target",
            base_branch="dev",
            workflow="gitflow",
        )
        store.insert_task(task)

        fetched = store.fetch_next()
        assert fetched is not None
        assert fetched.target_repo == "owner/target"
        assert fetched.base_branch == "dev"
        assert fetched.workflow == "gitflow"

    def test_defaults_when_not_specified(self, store: SQLiteStore) -> None:
        task = _make_task()
        store.insert_task(task)
        retrieved = store.get_task(task.id)
        assert retrieved is not None
        assert retrieved.target_repo == ""
        assert retrieved.base_branch == "main"
        assert retrieved.workflow == "trunk_based"


class TestMigrateV2:
    """_migrate_v2 backfills target_repo/installation_id/base_branch/workflow
    onto databases created before those columns existed (installation_id is
    dropped again by _migrate_v8)."""

    def _create_pre_fase5_db(self, db_path: str) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("""
                CREATE TABLE processed_deliveries (
                  delivery_id   TEXT PRIMARY KEY,
                  received_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  event_type    TEXT NOT NULL,
                  action        TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE tasks (
                  id              TEXT PRIMARY KEY,
                  delivery_id     TEXT NOT NULL,
                  repo            TEXT NOT NULL,
                  issue_number    INTEGER NOT NULL,
                  trigger_kind    TEXT NOT NULL,
                  label_category  TEXT,
                  status          TEXT NOT NULL,
                  pr_number       INTEGER,
                  cost_usd        REAL NOT NULL DEFAULT 0,
                  iterations      INTEGER NOT NULL DEFAULT 0,
                  retry_count     INTEGER NOT NULL DEFAULT 0,
                  error           TEXT,
                  created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  started_at      TEXT,
                  completed_at    TEXT,
                  heartbeat_at    TEXT,
                  payload_json    TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE UNIQUE INDEX idx_tasks_unique_pending "
                "ON tasks(repo, issue_number, trigger_kind) WHERE status = 'pending'"
            )
            conn.execute("CREATE INDEX idx_tasks_status ON tasks(status, created_at)")
            conn.execute(
                "CREATE INDEX idx_tasks_heartbeat "
                "ON tasks(status, heartbeat_at) WHERE status = 'running'"
            )
            conn.execute("""
                CREATE TABLE task_events (
                  id            INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id       TEXT NOT NULL REFERENCES tasks(id),
                  event_type    TEXT NOT NULL,
                  payload_json  TEXT NOT NULL,
                  created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX idx_task_events_task ON task_events(task_id, created_at)")
            conn.execute("""
                CREATE TABLE daily_budget (
                  date           TEXT PRIMARY KEY,
                  total_cost_usd REAL NOT NULL DEFAULT 0,
                  task_count     INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute(
                "INSERT INTO tasks "
                "(id, delivery_id, repo, issue_number, trigger_kind, status, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("t1", "d1", "owner/repo", 42, "issue_opened", "succeeded", "{}"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_backfills_target_repo_from_repo(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-fase5.db")
        self._create_pre_fase5_db(db_path)

        store = SQLiteStore(db_path)
        store.init()

        task = store.get_task("t1")
        assert task is not None
        assert task.target_repo == "owner/repo"
        assert task.base_branch == "main"
        assert task.workflow == "trunk_based"

    def test_idempotent_on_repeat_init(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-fase5-idempotent.db")
        self._create_pre_fase5_db(db_path)

        store = SQLiteStore(db_path)
        store.init()
        store.init()

        row = store.conn.execute("SELECT COUNT(*) AS cnt FROM tasks").fetchone()
        assert row["cnt"] == 1

    def test_new_db_already_has_columns_and_skips_migration(self, store: SQLiteStore) -> None:
        columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(tasks)").fetchall()}
        assert "target_repo" in columns
        assert "base_branch" in columns
        assert "workflow" in columns


class TestDailyBudget:
    def test_get_defaults_to_zero(self, store: SQLiteStore) -> None:
        budget = store.get_daily_budget("2026-05-16", "global")
        assert budget.total_cost_usd == 0.0
        assert budget.task_count == 0
        assert budget.scope == "global"

    def test_add_cost_creates_row(self, store: SQLiteStore) -> None:
        store.add_daily_budget("2026-05-16", "global", 0.42)
        budget = store.get_daily_budget("2026-05-16", "global")
        assert budget.total_cost_usd == 0.42
        assert budget.task_count == 1

    def test_add_cost_accumulates(self, store: SQLiteStore) -> None:
        store.add_daily_budget("2026-05-16", "global", 0.50)
        store.add_daily_budget("2026-05-16", "global", 1.00)
        budget = store.get_daily_budget("2026-05-16", "global")
        assert budget.total_cost_usd == 1.50
        assert budget.task_count == 2

    def test_different_dates_independent(self, store: SQLiteStore) -> None:
        store.add_daily_budget("2026-05-16", "global", 0.50)
        store.add_daily_budget("2026-05-17", "global", 1.00)
        b1 = store.get_daily_budget("2026-05-16", "global")
        assert b1.total_cost_usd == 0.50
        b2 = store.get_daily_budget("2026-05-17", "global")
        assert b2.total_cost_usd == 1.00

    def test_different_scopes_same_date_independent(self, store: SQLiteStore) -> None:
        """Two repos spending on the same day must not bleed into each
        other's total — a regression to a single global (date)-keyed row
        would make repo-a's spend visible under repo-b too."""
        store.add_daily_budget("2026-05-16", "owner/repo-a", 12.0)
        store.add_daily_budget("2026-05-16", "owner/repo-b", 3.0)
        store.add_daily_budget("2026-05-16", "owner/repo-a", 1.0)

        a = store.get_daily_budget("2026-05-16", "owner/repo-a")
        b = store.get_daily_budget("2026-05-16", "owner/repo-b")

        assert a.total_cost_usd == 13.0
        assert a.task_count == 2
        assert b.total_cost_usd == 3.0
        assert b.task_count == 1


class TestMigrateV3:
    """_migrate_v3 backfills scope='global' onto daily_budget rows written
    before per-repo budget scoping existed."""

    def _create_pre_v3_db(self, db_path: str) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("""
                CREATE TABLE daily_budget (
                  date           TEXT PRIMARY KEY,
                  total_cost_usd REAL NOT NULL DEFAULT 0,
                  task_count     INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute(
                "INSERT INTO daily_budget (date, total_cost_usd, task_count) VALUES (?, ?, ?)",
                ("2026-05-16", 9.99, 3),
            )
            conn.commit()
        finally:
            conn.close()

    def test_backfills_scope_global(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-v3.db")
        self._create_pre_v3_db(db_path)

        store = SQLiteStore(db_path)
        store.init()

        budget = store.get_daily_budget("2026-05-16", "global")
        assert budget.total_cost_usd == 9.99
        assert budget.task_count == 3
        assert budget.scope == "global"

    def test_idempotent_on_repeat_init(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-v3-idempotent.db")
        self._create_pre_v3_db(db_path)

        store = SQLiteStore(db_path)
        store.init()
        store.init()

        row = store.conn.execute("SELECT COUNT(*) AS cnt FROM daily_budget").fetchone()
        assert row["cnt"] == 1

    def test_new_db_already_has_scope_and_skips_migration(self, store: SQLiteStore) -> None:
        columns = {
            row["name"] for row in store.conn.execute("PRAGMA table_info(daily_budget)").fetchall()
        }
        assert "scope" in columns


class TestIssueWork:
    """Per-issue work state (`issue_work`), one row per (issue_repo,
    issue_number), spanning as many tasks as needed."""

    def test_get_missing_returns_none(self, store: SQLiteStore) -> None:
        assert store.get_issue_work("github", "owner/repo", 42) is None

    def test_open_creates_open_record_with_frozen_branch(self, store: SQLiteStore) -> None:
        work = store.open_issue_work(
            "github", "owner/repo", 42, "owner/target", "main", "agent/issue-42-fix-thing"
        )
        assert isinstance(work, IssueWork)
        assert work.issue_repo == "owner/repo"
        assert work.issue_number == 42
        assert work.target_repo == "owner/target"
        assert work.base_branch == "main"
        assert work.work_branch == "agent/issue-42-fix-thing"
        assert work.state == "open"
        assert work.attempts == 0
        assert work.closed_reason is None
        assert work.created_at != ""
        assert work.updated_at != ""

    def test_get_reads_back_the_opened_record(self, store: SQLiteStore) -> None:
        store.open_issue_work("github", "owner/repo", 42, "owner/target", "main", "b")
        got = store.get_issue_work("github", "owner/repo", 42)
        assert got is not None
        assert got.work_branch == "b"
        assert got.state == "open"

    def test_one_row_per_issue(self, store: SQLiteStore) -> None:
        store.open_issue_work("github", "owner/repo", 42, "owner/target", "main", "b1")
        with pytest.raises(sqlite3.IntegrityError):
            store.open_issue_work("github", "owner/repo", 42, "owner/target", "main", "b2")

    def test_same_number_different_repo_is_independent(self, store: SQLiteStore) -> None:
        store.open_issue_work("github", "owner/a", 42, "owner/a", "main", "ba")
        store.open_issue_work("github", "owner/b", 42, "owner/b", "main", "bb")
        assert store.get_issue_work("github", "owner/a", 42).work_branch == "ba"
        assert store.get_issue_work("github", "owner/b", 42).work_branch == "bb"

    def test_update_bumps_attempts_and_touches_updated_at(self, store: SQLiteStore) -> None:
        store.open_issue_work("github", "owner/repo", 42, "owner/target", "main", "b")
        before = store.get_issue_work("github", "owner/repo", 42)
        time.sleep(0.01)
        store.update_issue_work("github", "owner/repo", 42, attempts=1, last_task_id="task-abc")
        after = store.get_issue_work("github", "owner/repo", 42)
        assert after.attempts == 1
        assert after.last_task_id == "task-abc"
        assert after.updated_at > before.updated_at
        assert after.state == "open"  # untouched fields survive

    def test_update_with_no_fields_is_noop(self, store: SQLiteStore) -> None:
        store.open_issue_work("github", "owner/repo", 42, "owner/target", "main", "b")
        store.update_issue_work("github", "owner/repo", 42)
        assert store.get_issue_work("github", "owner/repo", 42).attempts == 0

    def test_close_marks_closed_with_reason(self, store: SQLiteStore) -> None:
        store.open_issue_work("github", "owner/repo", 42, "owner/target", "main", "b")
        store.close_issue_work("github", "owner/repo", 42, "no_changes")
        work = store.get_issue_work("github", "owner/repo", 42)
        assert work.state == "closed"
        assert work.closed_reason == "no_changes"
        assert work.pr_number is None

    def test_close_pr_opened_records_pr_number(self, store: SQLiteStore) -> None:
        store.open_issue_work("github", "owner/repo", 42, "owner/target", "main", "b")
        store.close_issue_work("github", "owner/repo", 42, "pr_opened", pr_number=101)
        work = store.get_issue_work("github", "owner/repo", 42)
        assert work.state == "closed"
        assert work.closed_reason == "pr_opened"
        assert work.pr_number == 101

    def test_reopen_after_close_starts_fresh(self, store: SQLiteStore) -> None:
        """A closed record is not resumed: opening again is a brand-new
        work item (attempts back to 0, a freshly frozen branch)."""
        store.open_issue_work("github", "owner/repo", 42, "owner/target", "main", "b1")
        store.update_issue_work("github", "owner/repo", 42, attempts=2)
        store.close_issue_work("github", "owner/repo", 42, "max_attempts")
        # caller deletes/replaces on reopen; store just needs to allow it
        store.conn.execute(
            "DELETE FROM issue_work WHERE issue_repo = ? AND issue_number = ?",
            ("owner/repo", 42),
        )
        store.conn.commit()
        fresh = store.open_issue_work("github", "owner/repo", 42, "owner/target", "main", "b2")
        assert fresh.attempts == 0
        assert fresh.work_branch == "b2"
        assert fresh.state == "open"


class TestIssueWorkLifecycleQueries:
    """`touch_issue_work` and `list_open_issue_work`."""

    def test_touch_bumps_updated_at_only(self, store: SQLiteStore) -> None:
        store.open_issue_work("github", "owner/repo", 1, "owner/t", "main", "b")
        store.update_issue_work("github", "owner/repo", 1, attempts=2)
        before = store.get_issue_work("github", "owner/repo", 1)
        time.sleep(0.01)

        store.touch_issue_work("github", "owner/repo", 1)

        after = store.get_issue_work("github", "owner/repo", 1)
        assert after.updated_at > before.updated_at
        assert after.attempts == 2
        assert after.state == "open"

    def test_touch_missing_row_does_not_raise(self, store: SQLiteStore) -> None:
        store.touch_issue_work("github", "owner/repo", 999)

    def test_list_open_returns_only_open_oldest_first(self, store: SQLiteStore) -> None:
        store.open_issue_work("github", "owner/repo", 1, "owner/t", "main", "b1")
        time.sleep(0.01)
        store.open_issue_work("github", "owner/repo", 2, "owner/t", "main", "b2")
        time.sleep(0.01)
        store.open_issue_work("github", "owner/repo", 3, "owner/t", "main", "b3")
        store.close_issue_work("github", "owner/repo", 2, "pr_opened", pr_number=7)

        rows = store.list_open_issue_work(10)

        assert [w.issue_number for w in rows] == [1, 3]

    def test_list_open_respects_limit(self, store: SQLiteStore) -> None:
        for n in range(5):
            store.open_issue_work("github", "owner/repo", n, "owner/t", "main", f"b{n}")
            time.sleep(0.005)

        rows = store.list_open_issue_work(2)

        assert [w.issue_number for w in rows] == [0, 1]

    def test_list_open_zero_limit_returns_empty(self, store: SQLiteStore) -> None:
        store.open_issue_work("github", "owner/repo", 1, "owner/t", "main", "b")
        assert store.list_open_issue_work(0) == []


class TestMigrateV4:
    """_migrate_v4 adds the `issue_work` table to databases created before
    it existed — a pure additive CREATE TABLE, never a rebuild of `tasks`."""

    def _create_pre_v4_db(self, db_path: str) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE processed_deliveries (
                  delivery_id TEXT PRIMARY KEY, received_at TEXT NOT NULL
                  DEFAULT CURRENT_TIMESTAMP, event_type TEXT NOT NULL, action TEXT
                );
                CREATE TABLE tasks (
                  id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL, repo TEXT NOT NULL,
                  issue_number INTEGER NOT NULL, trigger_kind TEXT NOT NULL,
                  label_category TEXT, status TEXT NOT NULL, pr_number INTEGER,
                  cost_usd REAL NOT NULL DEFAULT 0, iterations INTEGER NOT NULL DEFAULT 0,
                  retry_count INTEGER NOT NULL DEFAULT 0, error TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, started_at TEXT,
                  completed_at TEXT, heartbeat_at TEXT, payload_json TEXT NOT NULL,
                  target_repo TEXT NOT NULL DEFAULT '', installation_id INTEGER NOT NULL DEFAULT 0,
                  base_branch TEXT NOT NULL DEFAULT 'main', workflow TEXT NOT NULL DEFAULT 'trunk_based'
                );
                CREATE UNIQUE INDEX idx_tasks_unique_pending
                  ON tasks(repo, issue_number, trigger_kind) WHERE status = 'pending';
                CREATE INDEX idx_tasks_status ON tasks(status, created_at);
                CREATE INDEX idx_tasks_heartbeat ON tasks(status, heartbeat_at) WHERE status = 'running';
                CREATE TABLE task_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id TEXT NOT NULL REFERENCES tasks(id),
                  event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX idx_task_events_task ON task_events(task_id, created_at);
                CREATE TABLE daily_budget (
                  date TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'global',
                  total_cost_usd REAL NOT NULL DEFAULT 0, task_count INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY (date, scope)
                );
                """
            )
            conn.execute(
                "INSERT INTO tasks "
                "(id, delivery_id, repo, issue_number, trigger_kind, status, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("t1", "d1", "owner/repo", 42, "issue_opened", "succeeded", "{}"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_creates_issue_work_without_losing_tasks(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-v4.db")
        self._create_pre_v4_db(db_path)

        store = SQLiteStore(db_path)
        store.init()

        # tasks row survived — no positional rebuild happened
        assert store.get_task("t1") is not None
        # issue_work is now usable
        store.open_issue_work("github", "owner/repo", 42, "owner/repo", "main", "b")
        assert store.get_issue_work("github", "owner/repo", 42) is not None

    def test_idempotent_on_repeat_init(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-v4-idempotent.db")
        self._create_pre_v4_db(db_path)

        store = SQLiteStore(db_path)
        store.init()
        store.open_issue_work("github", "owner/repo", 42, "owner/repo", "main", "b")
        store.init()

        row = store.conn.execute("SELECT COUNT(*) AS cnt FROM issue_work").fetchone()
        assert row["cnt"] == 1

    def test_new_db_has_issue_work_table(self, store: SQLiteStore) -> None:
        names = {
            r["name"]
            for r in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "issue_work" in names


class TestMigrateV5:
    """_migrate_v5 adds `total_cost_usd`/`total_iterations` to `issue_work`
    on databases created before that pair of columns existed — additive
    `ALTER TABLE ... ADD COLUMN`, never a rebuild (same idempotent pattern
    as `_migrate_v3`)."""

    def _create_pre_v5_db(self, db_path: str) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE issue_work (
                  issue_repo        TEXT    NOT NULL,
                  issue_number      INTEGER NOT NULL,
                  target_repo       TEXT    NOT NULL,
                  base_branch       TEXT    NOT NULL,
                  work_branch       TEXT    NOT NULL,
                  state             TEXT    NOT NULL,
                  closed_reason     TEXT,
                  attempts          INTEGER NOT NULL DEFAULT 0,
                  last_task_id      TEXT,
                  last_error        TEXT,
                  last_summary      TEXT,
                  last_summary_kind TEXT    NOT NULL DEFAULT '',
                  pr_number         INTEGER,
                  created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY (issue_repo, issue_number)
                );
                """
            )
            conn.execute(
                "INSERT INTO issue_work "
                "(issue_repo, issue_number, target_repo, base_branch, work_branch, state) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("owner/repo", 42, "owner/repo", "main", "b", "open"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_adds_columns_without_losing_existing_row(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-v5.db")
        self._create_pre_v5_db(db_path)

        store = SQLiteStore(db_path)
        store.init()

        work = store.get_issue_work("github", "owner/repo", 42)
        assert work is not None
        assert work.total_cost_usd == 0.0
        assert work.total_iterations == 0

        store.update_issue_work(
            "github", "owner/repo", 42, total_cost_usd=0.02, total_iterations=40
        )
        work = store.get_issue_work("github", "owner/repo", 42)
        assert work is not None
        assert work.total_cost_usd == 0.02
        assert work.total_iterations == 40

    def test_idempotent_on_repeat_init(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-v5-idempotent.db")
        self._create_pre_v5_db(db_path)

        store = SQLiteStore(db_path)
        store.init()
        store.init()

        columns = {
            row["name"] for row in store.conn.execute("PRAGMA table_info(issue_work)").fetchall()
        }
        assert {"total_cost_usd", "total_iterations"} <= columns

    def test_new_db_has_total_columns(self, store: SQLiteStore) -> None:
        columns = {
            row["name"] for row in store.conn.execute("PRAGMA table_info(issue_work)").fetchall()
        }
        assert {"total_cost_usd", "total_iterations"} <= columns


class TestMigrateV6:
    """_migrate_v6 adds `trigger_json`/`provider_instance`/`credentials_ref`
    to `tasks` — additive `ALTER TABLE ... ADD COLUMN`, never a rebuild.
    Rows left without a trigger are failed by the worker's payload guard."""

    def _create_pre_v6_db(self, db_path: str) -> None:
        # The pre-v4 schema has the same `tasks` table as v5 (v4/v5 only
        # touched issue_work).
        TestMigrateV4()._create_pre_v4_db(db_path)

    def test_adds_columns_without_losing_existing_row(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-v6.db")
        self._create_pre_v6_db(db_path)

        store = SQLiteStore(db_path)
        store.init()

        task = store.get_task("t1")
        assert task is not None
        assert task.trigger_json is None
        assert task.trigger is None
        assert task.provider_instance == "github"
        assert task.credentials_ref is None

    def test_idempotent_on_repeat_init(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-v6-idempotent.db")
        self._create_pre_v6_db(db_path)

        store = SQLiteStore(db_path)
        store.init()
        store.init()

        columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(tasks)").fetchall()}
        assert {"trigger_json", "provider_instance", "credentials_ref"} <= columns

    def test_new_db_has_trigger_columns(self, store: SQLiteStore) -> None:
        columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(tasks)").fetchall()}
        assert {"trigger_json", "provider_instance", "credentials_ref"} <= columns


class TestMigrateV7:
    """_migrate_v7 keys `processed_deliveries` and `issue_work` by provider
    instance — a rebuild, since a PK can't change in place (the documented
    exception to the additive-migration rule) — and widens the pending-task index to the instance."""

    def _create_pre_v7_db(self, db_path: str) -> None:
        TestMigrateV4()._create_pre_v4_db(db_path)
        # Bring it to the v6 shape by hand (the last one without instance
        # keys) and seed one row per table the migration rebuilds.
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(
                """
                ALTER TABLE tasks ADD COLUMN trigger_json TEXT;
                ALTER TABLE tasks ADD COLUMN provider_instance TEXT NOT NULL DEFAULT 'github';
                ALTER TABLE tasks ADD COLUMN credentials_ref TEXT;
                CREATE TABLE issue_work (
                  issue_repo TEXT NOT NULL, issue_number INTEGER NOT NULL,
                  target_repo TEXT NOT NULL, base_branch TEXT NOT NULL,
                  work_branch TEXT NOT NULL, state TEXT NOT NULL,
                  closed_reason TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                  last_task_id TEXT, last_error TEXT, last_summary TEXT,
                  last_summary_kind TEXT NOT NULL DEFAULT '', pr_number INTEGER,
                  total_cost_usd REAL NOT NULL DEFAULT 0,
                  total_iterations INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY (issue_repo, issue_number)
                );
                CREATE INDEX idx_issue_work_open ON issue_work(state, updated_at)
                  WHERE state = 'open';
                INSERT INTO processed_deliveries (delivery_id, event_type, action)
                  VALUES ('d1', 'issues', 'opened');
                INSERT INTO issue_work
                  (issue_repo, issue_number, target_repo, base_branch, work_branch,
                   state, closed_reason, attempts, pr_number, total_cost_usd)
                  VALUES ('owner/repo', 42, 'owner/code', 'main', 'agent/issue-42',
                          'closed', 'pr_opened', 2, 7, 0.5);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _pk(self, store: SQLiteStore, table: str) -> list[str]:
        rows = store.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [r["name"] for r in sorted(rows, key=lambda r: r["pk"]) if r["pk"]]

    def test_rebuilds_tables_and_keeps_rows_on_github(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-v7.db")
        self._create_pre_v7_db(db_path)

        store = SQLiteStore(db_path)
        store.init()

        assert self._pk(store, "processed_deliveries") == [
            "provider_instance",
            "delivery_id",
        ]
        assert self._pk(store, "issue_work") == [
            "provider_instance",
            "issue_repo",
            "issue_number",
        ]
        assert store.is_delivery_processed("github", "d1")
        work = store.get_issue_work("github", "owner/repo", 42)
        assert work is not None
        assert work.provider_instance == "github"
        assert (work.closed_reason, work.attempts, work.pr_number) == ("pr_opened", 2, 7)
        assert work.total_cost_usd == 0.5
        assert store.get_task("t1") is not None

    def test_pending_index_covers_the_instance(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-v7-index.db")
        self._create_pre_v7_db(db_path)

        store = SQLiteStore(db_path)
        store.init()

        columns = [
            r["name"]
            for r in store.conn.execute("PRAGMA index_info(idx_tasks_unique_pending)").fetchall()
        ]
        assert columns == ["provider_instance", "repo", "issue_number", "trigger_kind"]

    def test_idempotent_on_repeat_init(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-v7-idempotent.db")
        self._create_pre_v7_db(db_path)

        store = SQLiteStore(db_path)
        store.init()
        store.init()

        assert store.conn.execute("SELECT COUNT(*) AS cnt FROM issue_work").fetchone()["cnt"] == 1
        assert (
            store.conn.execute("SELECT COUNT(*) AS cnt FROM processed_deliveries").fetchone()["cnt"]
            == 1
        )

    def test_new_db_already_has_the_new_keys(self, store: SQLiteStore) -> None:
        assert self._pk(store, "processed_deliveries") == [
            "provider_instance",
            "delivery_id",
        ]
        assert self._pk(store, "issue_work") == [
            "provider_instance",
            "issue_repo",
            "issue_number",
        ]


class TestMigrateV8:
    """_migrate_v8 drops the legacy `payload_json`/`installation_id` columns
    from `tasks` — a rebuild (a NOT NULL column can't be dropped in place),
    keeping every row and the `task_events` that point at them."""

    LEGACY = {"payload_json", "installation_id"}

    def _create_pre_v8_db(self, db_path: str) -> None:
        TestMigrateV7()._create_pre_v7_db(db_path)
        # One pending task with a trigger and an event, next to the
        # finished legacy row `t1` seeded by the v4 helper.
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(
                """
                INSERT INTO tasks
                  (id, delivery_id, repo, issue_number, trigger_kind, status,
                   payload_json, target_repo, installation_id, base_branch,
                   workflow, trigger_json, provider_instance, credentials_ref,
                   cost_usd, iterations)
                  VALUES ('t2', 'd2', 'owner/repo', 43, 'issue_opened', 'pending',
                          '{"raw": true}', 'owner/code', 99, 'develop',
                          'gitflow', '{"x": 1}', 'ghe', '99', 1.25, 4);
                INSERT INTO task_events (task_id, event_type, payload_json)
                  VALUES ('t2', 'started', '{"step": 1}');
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _columns(self, store: SQLiteStore) -> set[str]:
        return {r["name"] for r in store.conn.execute("PRAGMA table_info(tasks)").fetchall()}

    def _indexes(self, store: SQLiteStore) -> set[str]:
        return {
            r["name"]
            for r in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tasks'"
            ).fetchall()
        }

    def test_drops_legacy_columns_and_keeps_rows(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-v8.db")
        self._create_pre_v8_db(db_path)

        store = SQLiteStore(db_path)
        store.init()

        assert not self._columns(store) & self.LEGACY
        assert store.get_task("t1") is not None
        task = store.get_task("t2")
        assert task is not None
        assert (task.target_repo, task.base_branch, task.workflow) == (
            "owner/code",
            "develop",
            "gitflow",
        )
        assert (task.trigger_json, task.provider_instance, task.credentials_ref) == (
            '{"x": 1}',
            "ghe",
            "99",
        )
        assert (task.cost_usd, task.iterations) == (1.25, 4)
        assert [e.event_type for e in store.get_task_events("t2")] == ["started"]
        assert store.conn.execute("PRAGMA foreign_key_check").fetchall() == []

    def test_recreates_the_task_indexes(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-v8-index.db")
        self._create_pre_v8_db(db_path)

        store = SQLiteStore(db_path)
        store.init()

        assert {
            "idx_tasks_unique_pending",
            "idx_tasks_status",
            "idx_tasks_heartbeat",
        } <= self._indexes(store)
        columns = [
            r["name"]
            for r in store.conn.execute("PRAGMA index_info(idx_tasks_unique_pending)").fetchall()
        ]
        assert columns == ["provider_instance", "repo", "issue_number", "trigger_kind"]
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_task(
                _make_task(repo="owner/repo", issue_number=43, provider_instance="ghe")
            )

    def test_idempotent_on_repeat_init(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-v8-idempotent.db")
        self._create_pre_v8_db(db_path)

        store = SQLiteStore(db_path)
        store.init()
        store.init()

        assert store.conn.execute("SELECT COUNT(*) AS cnt FROM tasks").fetchone()["cnt"] == 2

    def test_full_chain_from_a_pre_v2_db(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-v2.db")
        TestMigrateV2()._create_pre_fase5_db(db_path)

        store = SQLiteStore(db_path)
        store.init()

        assert not self._columns(store) & self.LEGACY
        task = store.get_task("t1")
        assert task is not None and task.target_repo == "owner/repo"

    def test_new_db_has_no_legacy_columns(self, store: SQLiteStore) -> None:
        assert not self._columns(store) & self.LEGACY
        task_id = store.insert_task(_make_task())
        assert store.get_task(task_id) is not None


class TestProviderInstanceIsolation:
    """Two instances may share a delivery key, an issue slug and number."""

    def test_delivery_key_is_per_instance(self, store: SQLiteStore) -> None:
        store.mark_delivery_processed("github", "d1", "issues", "opened")

        assert store.is_delivery_processed("github", "d1")
        assert not store.is_delivery_processed("ghe", "d1")
        store.mark_delivery_processed("ghe", "d1", "issues", "opened")
        assert store.is_delivery_processed("ghe", "d1")

    def test_issue_work_is_per_instance(self, store: SQLiteStore) -> None:
        store.open_issue_work("github", "owner/repo", 1, "owner/repo", "main", "a")
        store.open_issue_work("ghe", "owner/repo", 1, "owner/repo", "main", "b")
        store.close_issue_work("ghe", "owner/repo", 1, "pr_opened")

        github = store.get_issue_work("github", "owner/repo", 1)
        ghe = store.get_issue_work("ghe", "owner/repo", 1)
        assert github is not None and github.state == "open"
        assert github.work_branch == "a"
        assert ghe is not None and ghe.state == "closed"
        assert ghe.provider_instance == "ghe"

    def test_pending_tasks_are_unique_per_instance(self, store: SQLiteStore) -> None:
        store.insert_task(_make_task(provider_instance="github"))
        store.insert_task(_make_task(provider_instance="ghe"))

        with pytest.raises(sqlite3.IntegrityError):
            store.insert_task(_make_task(provider_instance="ghe"))

    def test_active_creation_task_is_per_instance(self, store: SQLiteStore) -> None:
        store.insert_task(_make_task(provider_instance="github"))

        assert store.has_active_creation_task_for_issue("github", "stateway-io/stateway-issues", 42)
        assert not store.has_active_creation_task_for_issue(
            "ghe", "stateway-io/stateway-issues", 42
        )


class TestTriggerRoundtrip:
    def test_insert_and_fetch_preserve_the_trigger(self, store: SQLiteStore) -> None:
        trigger = make_trigger_event(TriggerKind.COMMENT_COMMAND)
        task_id = store.insert_task(make_task(trigger, credentials_ref="777"))

        fetched = store.fetch_next()

        assert fetched is not None and fetched.id == task_id
        assert fetched.trigger == trigger
        assert fetched.provider_instance == "github"
        assert fetched.credentials_ref == "777"


class TestCloseIssueWorkTotals:
    """close_issue_work persists the final cumulative cost/iterations for
    the work item, on top of the existing state/closed_reason/pr_number
    fields."""

    def test_close_persists_totals(self, store: SQLiteStore) -> None:
        store.open_issue_work("github", "owner/repo", 7, "owner/repo", "main", "b")

        store.close_issue_work(
            "github",
            "owner/repo",
            7,
            "pr_opened",
            pr_number=99,
            total_cost_usd=0.03,
            total_iterations=56,
        )

        work = store.get_issue_work("github", "owner/repo", 7)
        assert work is not None
        assert work.closed_reason == "pr_opened"
        assert work.pr_number == 99
        assert work.total_cost_usd == 0.03
        assert work.total_iterations == 56

    def test_close_without_totals_leaves_them_unchanged(self, store: SQLiteStore) -> None:
        store.open_issue_work("github", "owner/repo", 8, "owner/repo", "main", "b")
        store.update_issue_work("github", "owner/repo", 8, total_cost_usd=0.01, total_iterations=20)

        store.close_issue_work("github", "owner/repo", 8, "no_changes")

        work = store.get_issue_work("github", "owner/repo", 8)
        assert work is not None
        assert work.total_cost_usd == 0.01
        assert work.total_iterations == 20


class TestFetchNextFairness:
    """fetch_next does cheap round-robin between repos so one repo's
    backlog can't starve another repo's tasks that arrived later."""

    def test_second_repo_not_stuck_behind_first_repos_backlog(self, store: SQLiteStore) -> None:
        # 3 tasks from repo A, then 1 from repo B, all pending with
        # increasing created_at (insertion order == creation order here).
        for i in range(3):
            store.insert_task(
                _make_task(delivery_id=f"a-{i}", repo="owner/repo-a", issue_number=100 + i)
            )
        store.insert_task(_make_task(delivery_id="b-0", repo="owner/repo-b", issue_number=200))

        dispatched = []
        for _ in range(4):
            task = store.fetch_next()
            assert task is not None
            dispatched.append(task.repo)

        # A naive FIFO queue would dispatch repo-a 3 times before repo-b
        # ever gets a turn (repo-b at index 3). Fairness must pull it in
        # by at most the 2nd dispatch.
        assert "owner/repo-b" in dispatched[:2]
        assert sorted(dispatched) == sorted(
            ["owner/repo-a", "owner/repo-a", "owner/repo-a", "owner/repo-b"]
        )


class TestPRState:
    """The forge PR-state cache the retention sweep fills (`pr_state`)."""

    def _task_with_pr(self, store: SQLiteStore, pr: int, **overrides) -> str:
        fields = dict(
            delivery_id=f"d-{pr}-{overrides.get('issue_number', 42)}",
            target_repo="owner/code",
            issue_number=42,
        )
        fields.update(overrides)
        tid = store.insert_task(_make_task(**fields))
        store.update_status(tid, TaskStatus.SUCCEEDED, pr_number=pr)
        return tid

    def test_lists_prs_from_tasks_on_the_target_repo(self, store: SQLiteStore) -> None:
        self._task_with_pr(store, 7)
        assert store.list_prs_to_check(10) == [("github", "owner/code", 7)]

    def test_task_without_target_repo_falls_back_to_the_issue_repo(
        self, store: SQLiteStore
    ) -> None:
        self._task_with_pr(store, 7, target_repo="")
        assert store.list_prs_to_check(10) == [("github", "stateway-io/stateway-issues", 7)]

    def test_lists_prs_known_only_to_issue_work(self, store: SQLiteStore) -> None:
        store.open_issue_work("github", "owner/issues", 3, "owner/code", "main", "agent/issue-3")
        store.close_issue_work("github", "owner/issues", 3, "pr_opened", pr_number=11)
        assert store.list_prs_to_check(10) == [("github", "owner/code", 11)]

    def test_deduplicates_tasks_and_issue_work(self, store: SQLiteStore) -> None:
        self._task_with_pr(store, 7)
        self._task_with_pr(
            store, 7, delivery_id="d-followup", trigger_kind=TriggerKind.COMMENT_COMMAND
        )
        store.open_issue_work(
            "github", "stateway-io/stateway-issues", 42, "owner/code", "main", "agent/issue-42"
        )
        store.close_issue_work(
            "github", "stateway-io/stateway-issues", 42, "pr_opened", pr_number=7
        )
        assert store.list_prs_to_check(10) == [("github", "owner/code", 7)]

    def test_skips_prs_already_merged_or_closed(self, store: SQLiteStore) -> None:
        self._task_with_pr(store, 1, issue_number=1)
        self._task_with_pr(store, 2, issue_number=2)
        self._task_with_pr(store, 3, issue_number=3)
        store.upsert_pr_state("github", "owner/code", 1, "merged", "2026-10-01T00:00:00.000Z")
        store.upsert_pr_state("github", "owner/code", 2, "closed", "2026-10-01T00:00:00.000Z")
        store.upsert_pr_state("github", "owner/code", 3, "open", None)
        assert store.list_prs_to_check(10) == [("github", "owner/code", 3)]

    def test_never_checked_first_then_least_recently_checked(self, store: SQLiteStore) -> None:
        for pr in (1, 2, 3):
            self._task_with_pr(store, pr, issue_number=pr)
        store.upsert_pr_state("github", "owner/code", 1, "open", None)
        time.sleep(0.002)
        store.upsert_pr_state("github", "owner/code", 2, "open", None)
        assert store.list_prs_to_check(10) == [
            ("github", "owner/code", 3),
            ("github", "owner/code", 1),
            ("github", "owner/code", 2),
        ]
        assert store.list_prs_to_check(1) == [("github", "owner/code", 3)]

    def test_non_positive_limit_lists_nothing(self, store: SQLiteStore) -> None:
        self._task_with_pr(store, 7)
        assert store.list_prs_to_check(0) == []

    def test_upsert_overwrites_and_reads_back(self, store: SQLiteStore) -> None:
        store.upsert_pr_state("github", "owner/code", 7, "open", None)
        store.upsert_pr_state("github", "owner/code", 7, "merged", "2026-10-01T12:00:00.000Z")
        row = store.get_pr_state("github", "owner/code", 7)
        assert row is not None
        assert (row["state"], row["closed_at"]) == ("merged", "2026-10-01T12:00:00.000Z")
        assert row["checked_at"]
        assert store.get_pr_state("github", "owner/code", 8) is None

    def test_prunes_only_rows_nothing_points_to(self, store: SQLiteStore) -> None:
        self._task_with_pr(store, 1, issue_number=1)
        store.open_issue_work("github", "owner/issues", 2, "owner/code", "main", "agent/issue-2")
        store.close_issue_work("github", "owner/issues", 2, "pr_opened", pr_number=2)
        for pr in (1, 2, 3):
            store.upsert_pr_state("github", "owner/code", pr, "merged", None)

        assert store.delete_orphan_pr_states() == 1
        assert store.get_pr_state("github", "owner/code", 3) is None
        assert store.get_pr_state("github", "owner/code", 1) is not None
        assert store.get_pr_state("github", "owner/code", 2) is not None


class TestMigrateV9:
    """_migrate_v9 adds `pr_state` — purely additive, idempotent."""

    def test_adds_pr_state_to_a_db_without_it(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-v9.db")
        s = SQLiteStore(db_path)
        s.init()
        tid = s.insert_task(_make_task())
        s.conn.execute("DROP TABLE pr_state")
        s.conn.commit()
        s.close()

        s2 = SQLiteStore(db_path)
        s2.init()
        tables = {
            r["name"] for r in s2.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "pr_state" in tables
        assert s2.get_task(tid) is not None
        s2.init()  # idempotent
        s2.close()


class TestCIWatch:
    _KEY = ("github", "owner/issues", 7)

    def _watch(self, store: SQLiteStore, sha: str = "sha1", task: str = "t1"):
        return store.upsert_ci_watch(
            *self._KEY,
            target_repo="owner/code",
            pr_number=101,
            head_sha=sha,
            last_task_id=task,
        )

    def test_upsert_starts_watching(self, store: SQLiteStore) -> None:
        watch = self._watch(store)

        assert watch.state == "watching"
        assert (watch.target_repo, watch.pr_number, watch.head_sha) == ("owner/code", 101, "sha1")
        assert watch.last_task_id == "t1"
        assert watch.created_at
        assert watch.last_polled_at is None

    def test_a_new_push_replaces_the_watch(self, store: SQLiteStore) -> None:
        self._watch(store)
        store.update_ci_watch(*self._KEY, head_sha="sha1", state="fix_enqueued", fix_task_id="f")

        watch = self._watch(store, sha="sha2", task="t2")

        assert (watch.head_sha, watch.state, watch.fix_task_id) == ("sha2", "watching", None)
        assert watch.last_task_id == "t2"

    def test_update_only_applies_to_the_same_commit(self, store: SQLiteStore) -> None:
        self._watch(store, sha="sha2")

        assert store.update_ci_watch(*self._KEY, head_sha="sha1", state="passed") is False
        assert store.get_ci_watch(*self._KEY).state == "watching"
        assert store.update_ci_watch(*self._KEY, head_sha="sha2", state="passed") is True
        assert store.get_ci_watch(*self._KEY).state == "passed"

    def test_update_without_fields_is_a_no_op(self, store: SQLiteStore) -> None:
        self._watch(store)
        assert store.update_ci_watch(*self._KEY, head_sha="sha1") is False

    def test_lists_only_watching_never_polled_first(self, store: SQLiteStore) -> None:
        for n in (1, 2, 3):
            store.upsert_ci_watch(
                "github",
                "o/r",
                n,
                target_repo="o/r",
                pr_number=n,
                head_sha=f"s{n}",
                last_task_id=None,
            )
        store.update_ci_watch("github", "o/r", 1, head_sha="s1", last_polled_at="2026-01-02")
        store.update_ci_watch("github", "o/r", 2, head_sha="s2", state="passed")

        listed = store.list_watching_ci(10)

        assert [w.issue_number for w in listed] == [3, 1]
        assert store.list_watching_ci(0) == []

    def test_a_watching_issue_is_busy(self, store: SQLiteStore) -> None:
        self._watch(store)
        assert self._KEY in store.list_busy_issue_keys()

        store.update_ci_watch(*self._KEY, head_sha="sha1", state="passed")
        assert self._KEY not in store.list_busy_issue_keys()

    def test_finished_work_cleanup_drops_settled_watches_only(self, store: SQLiteStore) -> None:
        self._watch(store)
        store.delete_finished_issue_work([self._KEY])
        assert store.get_ci_watch(*self._KEY) is not None

        store.update_ci_watch(*self._KEY, head_sha="sha1", state="passed")
        store.delete_finished_issue_work([self._KEY])
        assert store.get_ci_watch(*self._KEY) is None


class TestMigrateV10:
    """_migrate_v10 adds `ci_watch` — purely additive, idempotent."""

    def test_adds_ci_watch_to_a_db_without_it(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre-v10.db")
        s = SQLiteStore(db_path)
        s.init()
        tid = s.insert_task(_make_task())
        s.conn.execute("DROP TABLE ci_watch")
        s.conn.commit()
        s.close()

        s2 = SQLiteStore(db_path)
        s2.init()
        tables = {
            r["name"] for r in s2.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "ci_watch" in tables
        assert s2.get_task(tid) is not None
        s2.init()  # idempotent
        s2.close()
