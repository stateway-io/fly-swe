from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from issue_agent.queue.models import (
    DailyBudget,
    IssueWork,
    Task,
    TaskEvent,
    TaskStatus,
    TriggerKind,
)

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_TERMINAL_STATUSES = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED_RECOVERABLE,
    TaskStatus.FAILED_TERMINAL,
    TaskStatus.ABORTED_BUDGET,
    TaskStatus.ABORTED_POLICY,
}

# Trigger kinds GitHub can fire together, in no guaranteed order, for a
# single issue creation event (an issue created with labels already
# attached fires both `issues.opened` and `issues.labeled`). Only one may
# turn into a task per issue at a time — see
# has_active_creation_task_for_issue()/insert_task_unless_active_creation_task().
_CREATION_TRIGGER_KINDS = (TriggerKind.ISSUE_OPENED.value, TriggerKind.LABEL_ADDED.value)


def _utcnow() -> str:
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond:06d}"[:3] + "Z"


def _generate_id() -> str:
    return uuid.uuid4().hex


class SQLiteStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._last_repo: str | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
        return self._conn

    def init(self) -> None:
        schema = _SCHEMA_PATH.read_text()
        schema = schema.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
        schema = schema.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ")
        self.conn.executescript(schema)
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.commit()
        self._migrate_v1()
        self._migrate_v2()
        self._migrate_v3()
        self._migrate_v4()
        self._migrate_v5()
        self._migrate_v6()
        self._migrate_v7()
        self._migrate_v8()

    def _migrate_v8(self) -> None:
        """Drop the GitHub-era `payload_json` and `installation_id` columns
        from `tasks` (PRD §3.6 cleanup): the canonical `trigger_json`
        replaced the raw payload, and credentials are resolved per repo by
        the provider plugin.

        `DROP COLUMN` can't remove a NOT NULL column without a default, so
        `tasks` is rebuilt v2-style in one transaction, keeping every row
        and the ids `task_events` points to. Like v7, this is a documented
        exception to the additive-migration rule (P4, see
        docs/operations.md): an older image can't insert into the new
        shape, so roll back by restoring a volume snapshot. Idempotent:
        guarded by `PRAGMA table_info`, a no-op on fresh DBs where
        schema.sql already created the new shape."""
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if not columns & {"payload_json", "installation_id"}:
            return

        kept = (
            "id, delivery_id, repo, issue_number, trigger_kind, label_category, "
            "status, pr_number, cost_usd, iterations, retry_count, error, "
            "created_at, started_at, completed_at, heartbeat_at, target_repo, "
            "base_branch, workflow, trigger_json, provider_instance, credentials_ref"
        )
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("""
                CREATE TABLE tasks_v8 (
                  id                TEXT PRIMARY KEY,
                  delivery_id       TEXT NOT NULL,
                  repo              TEXT NOT NULL,
                  issue_number      INTEGER NOT NULL,
                  trigger_kind      TEXT NOT NULL,
                  label_category    TEXT,
                  status            TEXT NOT NULL,
                  pr_number         INTEGER,
                  cost_usd          REAL NOT NULL DEFAULT 0,
                  iterations        INTEGER NOT NULL DEFAULT 0,
                  retry_count       INTEGER NOT NULL DEFAULT 0,
                  error             TEXT,
                  created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  started_at        TEXT,
                  completed_at      TEXT,
                  heartbeat_at      TEXT,
                  target_repo       TEXT NOT NULL DEFAULT '',
                  base_branch       TEXT NOT NULL DEFAULT 'main',
                  workflow          TEXT NOT NULL DEFAULT 'trunk_based',
                  trigger_json      TEXT,
                  provider_instance TEXT NOT NULL DEFAULT 'github',
                  credentials_ref   TEXT
                )
            """)
            self.conn.execute(f"INSERT INTO tasks_v8 ({kept}) SELECT {kept} FROM tasks")
            self.conn.execute("DROP TABLE tasks")
            self.conn.execute("ALTER TABLE tasks_v8 RENAME TO tasks")
            self.conn.execute(
                "CREATE UNIQUE INDEX idx_tasks_unique_pending ON tasks"
                "(provider_instance, repo, issue_number, trigger_kind) "
                "WHERE status = 'pending'"
            )
            self.conn.execute("CREATE INDEX idx_tasks_status ON tasks(status, created_at)")
            self.conn.execute(
                "CREATE INDEX idx_tasks_heartbeat "
                "ON tasks(status, heartbeat_at) WHERE status = 'running'"
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

    def _migrate_v7(self) -> None:
        """Key `processed_deliveries` and `issue_work` by provider instance
        (PRD §3.6), and add the instance to the pending-task unique index.

        A primary key can't be changed in place, so both tables are rebuilt
        v2-style (create, copy every row, drop, rename) in one transaction,
        with existing rows assigned to the `github` instance — the only one
        that existed before. This is the documented exception to the
        additive-migration rule (P4, see docs/operations.md). Idempotent:
        guarded by `PRAGMA table_info`, a no-op on fresh DBs where
        schema.sql already created the new shape.

        The pending index keeps its name, so schema.sql's `CREATE INDEX IF
        NOT EXISTS` can't replace an old definition; it is dropped and
        recreated whenever it doesn't cover `provider_instance` yet."""
        deliveries = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(processed_deliveries)").fetchall()
        }
        work = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(issue_work)").fetchall()
        }
        index_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA index_info(idx_tasks_unique_pending)").fetchall()
        }
        if (
            "provider_instance" in deliveries
            and "provider_instance" in work
            and "provider_instance" in index_columns
        ):
            return

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if "provider_instance" not in deliveries:
                self.conn.execute("""
                    CREATE TABLE processed_deliveries_v7 (
                      provider_instance TEXT NOT NULL DEFAULT 'github',
                      delivery_id   TEXT NOT NULL,
                      received_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      event_type    TEXT NOT NULL,
                      action        TEXT,
                      PRIMARY KEY (provider_instance, delivery_id)
                    )
                """)
                self.conn.execute("""
                    INSERT INTO processed_deliveries_v7
                      (provider_instance, delivery_id, received_at, event_type, action)
                    SELECT 'github', delivery_id, received_at, event_type, action
                    FROM processed_deliveries
                """)
                self.conn.execute("DROP TABLE processed_deliveries")
                self.conn.execute(
                    "ALTER TABLE processed_deliveries_v7 RENAME TO processed_deliveries"
                )
            if "provider_instance" not in work:
                self.conn.execute("""
                    CREATE TABLE issue_work_v7 (
                      provider_instance TEXT    NOT NULL DEFAULT 'github',
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
                      total_cost_usd    REAL    NOT NULL DEFAULT 0,
                      total_iterations  INTEGER NOT NULL DEFAULT 0,
                      created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      PRIMARY KEY (provider_instance, issue_repo, issue_number)
                    )
                """)
                columns = (
                    "issue_repo, issue_number, target_repo, base_branch, "
                    "work_branch, state, closed_reason, attempts, last_task_id, "
                    "last_error, last_summary, last_summary_kind, pr_number, "
                    "total_cost_usd, total_iterations, created_at, updated_at"
                )
                self.conn.execute(
                    f"INSERT INTO issue_work_v7 (provider_instance, {columns}) "
                    f"SELECT 'github', {columns} FROM issue_work"
                )
                self.conn.execute("DROP TABLE issue_work")
                self.conn.execute("ALTER TABLE issue_work_v7 RENAME TO issue_work")
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_issue_work_open "
                    "ON issue_work(state, updated_at) WHERE state = 'open'"
                )
            if "provider_instance" not in index_columns:
                self.conn.execute("DROP INDEX IF EXISTS idx_tasks_unique_pending")
                self.conn.execute(
                    "CREATE UNIQUE INDEX idx_tasks_unique_pending ON tasks"
                    "(provider_instance, repo, issue_number, trigger_kind) "
                    "WHERE status = 'pending'"
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _migrate_v6(self) -> None:
        """Add the canonical-model columns to `tasks` (PRD §3.6): the
        serialized `TriggerEvent`, the provider instance and the opaque
        credentials reference that will replace `installation_id`.

        Additive `ALTER TABLE ... ADD COLUMN`, guarded by `PRAGMA
        table_info` like `_migrate_v5` — idempotent, including on fresh DBs
        where schema.sql already created the columns. Existing rows keep a
        NULL `trigger_json`; the worker's payload guard fails them."""
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "trigger_json" not in columns:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN trigger_json TEXT")
        if "provider_instance" not in columns:
            self.conn.execute(
                "ALTER TABLE tasks ADD COLUMN provider_instance TEXT NOT NULL DEFAULT 'github'"
            )
        if "credentials_ref" not in columns:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN credentials_ref TEXT")
        self.conn.commit()

    def _migrate_v5(self) -> None:
        """Add `total_cost_usd`/`total_iterations` to `issue_work`, summed
        across every attempt of a work item, so a resumed run's own (often
        tiny) per-attempt cost isn't mistaken for the issue's total spend.

        Additive `ALTER TABLE ... ADD COLUMN`, guarded by `PRAGMA
        table_info` like `_migrate_v3` — idempotent, including on fresh DBs
        where schema.sql already created the columns."""
        columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(issue_work)").fetchall()
        }
        if "total_cost_usd" not in columns:
            self.conn.execute(
                "ALTER TABLE issue_work ADD COLUMN total_cost_usd REAL NOT NULL DEFAULT 0"
            )
        if "total_iterations" not in columns:
            self.conn.execute(
                "ALTER TABLE issue_work ADD COLUMN total_iterations INTEGER NOT NULL DEFAULT 0"
            )
        self.conn.commit()

    def _migrate_v4(self) -> None:
        """Add `issue_work` (per-issue work state) to databases that predate it.

        Unlike _migrate_v1.._migrate_v3, this never rebuilds `tasks`: it is
        only ``CREATE TABLE IF NOT EXISTS`` plus its partial index, the
        additive form the additive-migration rule requires (P4). Idempotent —
        a no-op once the table exists (including on fresh DBs, where
        schema.sql already created it)."""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS issue_work (
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
            CREATE INDEX IF NOT EXISTS idx_issue_work_open
              ON issue_work(state, updated_at) WHERE state = 'open';
            """
        )
        self.conn.commit()

    def _migrate_v3(self) -> None:
        """Add scope to daily_budget, with composite PK (date, scope)."""
        columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(daily_budget)").fetchall()
        }
        if "scope" in columns:
            return

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("""
                CREATE TABLE daily_budget_v2 (
                    date TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'global',
                    total_cost_usd REAL NOT NULL DEFAULT 0,
                    task_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (date, scope)
                )
            """)
            self.conn.execute("""
                INSERT INTO daily_budget_v2 (date, scope, total_cost_usd, task_count)
                SELECT date, 'global', total_cost_usd, task_count FROM daily_budget
            """)
            self.conn.execute("DROP TABLE daily_budget")
            self.conn.execute("ALTER TABLE daily_budget_v2 RENAME TO daily_budget")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _migrate_v2(self) -> None:
        """Add target_repo/installation_id/base_branch/workflow to tasks."""
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "target_repo" in columns:
            return

        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("""
                CREATE TABLE tasks_v3 (
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
                    payload_json    TEXT NOT NULL,
                    target_repo     TEXT NOT NULL DEFAULT '',
                    installation_id INTEGER NOT NULL DEFAULT 0,
                    base_branch     TEXT NOT NULL DEFAULT 'main',
                    workflow        TEXT NOT NULL DEFAULT 'trunk_based'
                )
            """)
            self.conn.execute("""
                INSERT INTO tasks_v3 (
                    id, delivery_id, repo, issue_number, trigger_kind,
                    label_category, status, pr_number, cost_usd, iterations,
                    retry_count, error, created_at, started_at, completed_at,
                    heartbeat_at, payload_json,
                    target_repo, installation_id, base_branch, workflow
                )
                SELECT
                    id, delivery_id, repo, issue_number, trigger_kind,
                    label_category, status, pr_number, cost_usd, iterations,
                    retry_count, error, created_at, started_at, completed_at,
                    heartbeat_at, payload_json,
                    repo, 0, 'main', 'trunk_based'
                FROM tasks
            """)
            self.conn.execute("DROP TABLE tasks")
            self.conn.execute("ALTER TABLE tasks_v3 RENAME TO tasks")
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_unique_pending "
                "ON tasks(repo, issue_number, trigger_kind) WHERE status = 'pending'"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_heartbeat "
                "ON tasks(status, heartbeat_at) WHERE status = 'running'"
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

    def _migrate_v1(self) -> None:
        """Remove old table-level UNIQUE constraint, add partial unique index."""
        indexes = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tasks' AND name LIKE 'sqlite_autoindex_tasks_%'"
        ).fetchall()
        has_old_constraint = len(indexes) > 1
        if not has_old_constraint:
            return

        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("""
                CREATE TABLE tasks_v2 (
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
            self.conn.execute("INSERT INTO tasks_v2 SELECT * FROM tasks")
            self.conn.execute("DROP TABLE tasks")
            self.conn.execute("ALTER TABLE tasks_v2 RENAME TO tasks")
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_unique_pending "
                "ON tasks(repo, issue_number, trigger_kind) WHERE status = 'pending'"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_heartbeat "
                "ON tasks(status, heartbeat_at) WHERE status = 'running'"
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── deliveries (idempotency) ────────────────────────────────

    def is_delivery_processed(self, provider_instance: str, delivery_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM processed_deliveries WHERE provider_instance = ? AND delivery_id = ?",
            (provider_instance, delivery_id),
        ).fetchone()
        return row is not None

    def mark_delivery_processed(
        self,
        provider_instance: str,
        delivery_id: str,
        event_type: str,
        action: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO processed_deliveries "
            "(provider_instance, delivery_id, event_type, action) VALUES (?, ?, ?, ?)",
            (provider_instance, delivery_id, event_type, action),
        )
        self.conn.commit()

    # ── tasks ───────────────────────────────────────────────────

    def _insert_task_row(self, task: Task) -> None:
        task.id = _generate_id()
        task.created_at = _utcnow()
        self.conn.execute(
            """INSERT INTO tasks
               (id, delivery_id, repo, issue_number, trigger_kind,
                label_category, status, created_at,
                target_repo, base_branch, workflow,
                trigger_json, provider_instance, credentials_ref)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task.id,
                task.delivery_id,
                task.repo,
                task.issue_number,
                task.trigger_kind.value,
                task.label_category,
                task.status.value,
                task.created_at,
                task.target_repo,
                task.base_branch,
                task.workflow,
                task.trigger_json,
                task.provider_instance,
                task.credentials_ref,
            ),
        )

    def insert_task(self, task: Task) -> str:
        self._insert_task_row(task)
        self.conn.commit()
        return task.id

    def insert_task_unless_active_creation_task(self, task: Task) -> str | None:
        """Atomically insert `task` (an issue_opened/label_added task)
        unless another creation-trigger task is already pending/running for
        the same issue. Returns the new task id, or ``None`` if the insert
        was skipped.

        GitHub fires `issues.opened` and `issues.labeled` almost
        simultaneously, with no guaranteed order, when an issue is created
        with labels already attached. A plain check-then-insert
        (has_active_creation_task_for_issue() followed by insert_task())
        has a race: both webhook handlers can pass the check before either
        commits, producing two tasks for the same issue. `BEGIN IMMEDIATE`
        (as in fetch_next()) acquires the write lock before the check runs,
        so whichever of the two webhooks reaches this method second always
        sees the first one's row and is turned away, regardless of which
        was processed first.
        """
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            if self.has_active_creation_task_for_issue(
                task.provider_instance, task.repo, task.issue_number
            ):
                conn.commit()
                return None
            self._insert_task_row(task)
            conn.commit()
            return task.id
        except Exception:
            conn.rollback()
            raise

    def fetch_next(self) -> Task | None:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT id, repo FROM tasks WHERE status = ? "
                "ORDER BY (repo = ?), created_at LIMIT 1",
                (TaskStatus.PENDING.value, self._last_repo or ""),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            task_id = row["id"]
            self._last_repo = row["repo"]
            now = _utcnow()
            conn.execute(
                "UPDATE tasks SET status = ?, started_at = ?, heartbeat_at = ? WHERE id = ?",
                (TaskStatus.RUNNING.value, now, now, task_id),
            )
            task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            conn.commit()
            return Task(**dict(task_row))
        except Exception:
            conn.rollback()
            raise

    def update_status(self, task_id: str, status: TaskStatus, **extra: Any) -> None:
        now = _utcnow()
        fields: dict[str, Any] = {"status": status.value}

        if status is TaskStatus.RUNNING:
            fields.setdefault("started_at", now)
            fields["heartbeat_at"] = now
        elif status in _TERMINAL_STATUSES:
            fields.setdefault("completed_at", now)

        fields.update({k: v for k, v in extra.items() if v is not None})

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [task_id]
        self.conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        self.conn.commit()

    def heartbeat(self, task_id: str) -> None:
        self.conn.execute(
            "UPDATE tasks SET heartbeat_at = ? WHERE id = ?",
            (_utcnow(), task_id),
        )
        self.conn.commit()

    def get_task(self, task_id: str) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return Task(**dict(row)) if row else None

    def get_queue_depth(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE status = ?",
            (TaskStatus.PENDING.value,),
        ).fetchone()
        return row["cnt"] if row else 0

    def has_active_creation_task_for_issue(
        self, provider_instance: str, repo: str, issue_number: int
    ) -> bool:
        """True when a pending/running issue_opened or label_added task
        already exists for this issue.

        Scoped to those two trigger kinds only — a comment_command task
        that happens to be in flight for the same issue never counts, so a
        genuinely new, unrelated `issues.labeled` event later isn't
        silently swallowed just because some other task is active on the
        issue. See insert_task_unless_active_creation_task() for the
        atomic check-and-insert this backs.
        """
        row = self.conn.execute(
            "SELECT 1 FROM tasks WHERE provider_instance = ? AND repo = ? "
            "AND issue_number = ? "
            "AND status IN (?, ?) AND trigger_kind IN (?, ?) LIMIT 1",
            (
                provider_instance,
                repo,
                issue_number,
                TaskStatus.PENDING.value,
                TaskStatus.RUNNING.value,
                *_CREATION_TRIGGER_KINDS,
            ),
        ).fetchone()
        return row is not None

    def find_orphaned_tasks(self, heartbeat_threshold: str) -> list[Task]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE status = ? AND heartbeat_at < ?",
            (TaskStatus.RUNNING.value, heartbeat_threshold),
        ).fetchall()
        return [Task(**dict(r)) for r in rows]

    def find_recoverable_tasks(self) -> list[Task]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY completed_at",
            (TaskStatus.FAILED_RECOVERABLE.value,),
        ).fetchall()
        return [Task(**dict(r)) for r in rows]

    def reset_to_pending(self, task_id: str, new_retry_count: int) -> None:
        self.conn.execute(
            """UPDATE tasks SET status=?, started_at=NULL,
               heartbeat_at=NULL, completed_at=NULL, error=NULL,
               retry_count=?
               WHERE id=?""",
            (TaskStatus.PENDING.value, new_retry_count, task_id),
        )
        self.conn.commit()

    # ── cleanup / retention ─────────────────────────────────────

    def find_completed_task_ids_older_than(self, cutoff: str) -> list[str]:
        rows = self.conn.execute(
            """SELECT id FROM tasks
               WHERE status IN (?, ?, ?, ?, ?)
               AND completed_at IS NOT NULL
               AND completed_at < ?""",
            (
                TaskStatus.SUCCEEDED.value,
                TaskStatus.FAILED_RECOVERABLE.value,
                TaskStatus.FAILED_TERMINAL.value,
                TaskStatus.ABORTED_BUDGET.value,
                TaskStatus.ABORTED_POLICY.value,
                cutoff,
            ),
        ).fetchall()
        return [r["id"] for r in rows]

    def find_running_task_ids(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT id FROM tasks WHERE status = ?",
            (TaskStatus.RUNNING.value,),
        ).fetchall()
        return {r["id"] for r in rows}

    def delete_old_records(self, task_ids: list[str], cutoff: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        if not task_ids:
            return counts

        placeholders = ",".join("?" for _ in task_ids)

        cur = self.conn.execute(
            f"DELETE FROM task_events WHERE task_id IN ({placeholders})",
            task_ids,
        )
        counts["task_events"] = cur.rowcount

        cur = self.conn.execute(
            f"DELETE FROM tasks WHERE id IN ({placeholders})",
            task_ids,
        )
        counts["tasks"] = cur.rowcount

        cur = self.conn.execute(
            "DELETE FROM processed_deliveries WHERE received_at < ?",
            (cutoff,),
        )
        counts["processed_deliveries"] = cur.rowcount

        cur = self.conn.execute(
            "DELETE FROM daily_budget WHERE date < ?",
            (cutoff[:10],),  # YYYY-MM-DD part only
        )
        counts["daily_budget"] = cur.rowcount

        self.conn.commit()
        return counts

    # ── task events ─────────────────────────────────────────────

    def append_task_event(self, task_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO task_events (task_id, event_type, payload_json) VALUES (?, ?, ?)",
            (task_id, event_type, json.dumps(payload)),
        )
        self.conn.commit()

    def get_task_events(self, task_id: str) -> list[TaskEvent]:
        rows = self.conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        ).fetchall()
        return [TaskEvent(**dict(r)) for r in rows]

    # ── daily budget ────────────────────────────────────────────

    def get_daily_budget(self, date: str, scope: str) -> DailyBudget:
        row = self.conn.execute(
            "SELECT * FROM daily_budget WHERE date = ? AND scope = ?", (date, scope)
        ).fetchone()
        if row is None:
            return DailyBudget(date=date, scope=scope)
        return DailyBudget(**dict(row))

    def add_daily_budget(self, date: str, scope: str, cost_usd: float) -> None:
        self.conn.execute(
            """INSERT INTO daily_budget (date, scope, total_cost_usd, task_count)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(date, scope) DO UPDATE SET
                   total_cost_usd = total_cost_usd + excluded.total_cost_usd,
                   task_count = task_count + 1""",
            (date, scope, cost_usd),
        )
        self.conn.commit()

    # ── issue work (per-issue business state) ───────────────────

    def get_issue_work(
        self, provider_instance: str, issue_repo: str, issue_number: int
    ) -> IssueWork | None:
        row = self.conn.execute(
            "SELECT * FROM issue_work "
            "WHERE provider_instance = ? AND issue_repo = ? AND issue_number = ?",
            (provider_instance, issue_repo, issue_number),
        ).fetchone()
        return IssueWork(**dict(row)) if row else None

    def open_issue_work(
        self,
        provider_instance: str,
        issue_repo: str,
        issue_number: int,
        target_repo: str,
        base_branch: str,
        work_branch: str,
    ) -> IssueWork:
        now = _utcnow()
        self.conn.execute(
            """INSERT INTO issue_work
               (provider_instance, issue_repo, issue_number, target_repo,
                base_branch, work_branch, state, attempts, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'open', 0, ?, ?)""",
            (
                provider_instance,
                issue_repo,
                issue_number,
                target_repo,
                base_branch,
                work_branch,
                now,
                now,
            ),
        )
        self.conn.commit()
        work = self.get_issue_work(provider_instance, issue_repo, issue_number)
        assert work is not None  # just inserted
        return work

    def update_issue_work(
        self, provider_instance: str, issue_repo: str, issue_number: int, **fields: Any
    ) -> None:
        if not fields:
            return
        fields["updated_at"] = _utcnow()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [provider_instance, issue_repo, issue_number]
        self.conn.execute(
            f"UPDATE issue_work SET {set_clause} "
            "WHERE provider_instance = ? AND issue_repo = ? AND issue_number = ?",
            values,
        )
        self.conn.commit()

    def delete_issue_work(self, provider_instance: str, issue_repo: str, issue_number: int) -> None:
        """Drop the row entirely — used when a closed work item is replaced by
        a fresh one on a new trigger (the PK forbids a second ``open``)."""
        self.conn.execute(
            "DELETE FROM issue_work "
            "WHERE provider_instance = ? AND issue_repo = ? AND issue_number = ?",
            (provider_instance, issue_repo, issue_number),
        )
        self.conn.commit()

    def touch_issue_work(self, provider_instance: str, issue_repo: str, issue_number: int) -> None:
        """Bump ``updated_at`` without changing any other field — records that
        an external event (e.g. ``issues.reopened``, R7.2) was seen."""
        self.conn.execute(
            "UPDATE issue_work SET updated_at = ? "
            "WHERE provider_instance = ? AND issue_repo = ? AND issue_number = ?",
            (_utcnow(), provider_instance, issue_repo, issue_number),
        )
        self.conn.commit()

    def list_open_issue_work(self, limit: int) -> list[IssueWork]:
        """Open work rows, oldest first — the reconciliation sweep's input
        (R7.3/R7.4). Served by ``idx_issue_work_open``."""
        if limit <= 0:
            return []
        rows = self.conn.execute(
            "SELECT * FROM issue_work WHERE state = 'open' ORDER BY updated_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [IssueWork(**dict(r)) for r in rows]

    def close_issue_work(
        self,
        provider_instance: str,
        issue_repo: str,
        issue_number: int,
        closed_reason: str,
        pr_number: int | None = None,
        total_cost_usd: float | None = None,
        total_iterations: int | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "state": "closed",
            "closed_reason": closed_reason,
        }
        if pr_number is not None:
            fields["pr_number"] = pr_number
        if total_cost_usd is not None:
            fields["total_cost_usd"] = total_cost_usd
        if total_iterations is not None:
            fields["total_iterations"] = total_iterations
        self.update_issue_work(provider_instance, issue_repo, issue_number, **fields)
