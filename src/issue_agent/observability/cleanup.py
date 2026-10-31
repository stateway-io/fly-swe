from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from issue_agent.logging import bind_context, get_logger
from issue_agent.observability.audit import prune_old_audit_files, write_audit_entry
from issue_agent.observability.prometheus_metrics import observe_issue_work_closed

if TYPE_CHECKING:
    from collections.abc import Callable

    from issue_agent.config import Settings
    from issue_agent.providers.registry import ProviderRegistry
    from issue_agent.queue.store import SQLiteStore

logger = get_logger(__name__)


@dataclass
class MaintenanceResult:
    events_removed: int = 0
    workspaces_removed: int = 0
    audit_files_pruned: int = 0
    db_task_events: int = 0
    db_tasks: int = 0
    db_deliveries: int = 0
    db_budget: int = 0

    @property
    def total(self) -> int:
        return (
            self.events_removed
            + self.workspaces_removed
            + self.audit_files_pruned
            + self.db_task_events
            + self.db_tasks
            + self.db_deliveries
            + self.db_budget
        )


@dataclass
class ReconcileResult:
    checked: int = 0
    closed: int = 0


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _default_issue_state_fn(
    registry: ProviderRegistry,
) -> Callable[[str, str, int], str]:
    """Build the real "is this issue closed on the forge?" probe (R7.3): each
    row is looked up on the provider instance that recorded it. An instance
    no longer configured raises ``KeyError``, which fails only that row."""

    def _state(provider_instance: str, issue_repo: str, issue_number: int) -> str:
        plugin = registry.get(provider_instance)
        return plugin.client(issue_repo).get_issue_state(issue_number)

    return _state


def reconcile_issue_work(
    store: SQLiteStore,
    settings: Settings,
    registry: ProviderRegistry,
    *,
    issue_state_fn: Callable[[str, str, int], str] | None = None,
) -> ReconcileResult:
    """Close every open ``issue_work`` whose issue is already closed on the forge
    (R7.3). This is the safety net for a missed ``issues.closed`` webhook or a
    service outage — not the primary path (R7.5), which is the zero-cost
    handler. Bounded by ``resume_reconcile_max_per_cycle`` forge lookups per
    call (R7.4); a per-row failure is logged and the sweep continues. Like
    ``run_maintenance``, the whole sweep is best-effort — it never propagates."""
    result = ReconcileResult()
    try:
        rows = store.list_open_issue_work(settings.resume_reconcile_max_per_cycle)
        if not rows:
            return result
        state_fn = issue_state_fn or _default_issue_state_fn(registry)
    except Exception:
        bind_context(logger).exception("issue_work_reconcile_setup_failed")
        return result

    for w in rows:
        row_log = bind_context(
            logger,
            provider_instance=w.provider_instance,
            issue_repo=w.issue_repo,
            issue_number=w.issue_number,
        )
        try:
            state = state_fn(w.provider_instance, w.issue_repo, w.issue_number)
            result.checked += 1
            if state != "closed":
                continue
            store.close_issue_work(
                w.provider_instance, w.issue_repo, w.issue_number, "issue_closed"
            )
            observe_issue_work_closed(
                w.target_repo, w.attempts, provider_instance=w.provider_instance
            )
            write_audit_entry(
                settings.data_dir,
                {
                    "event": "issue_work_reconciled",
                    "issue_repo": w.issue_repo,
                    "provider_instance": w.provider_instance,
                    "issue_number": w.issue_number,
                    "closed_reason": "issue_closed",
                    "attempts": w.attempts,
                    "work_branch": w.work_branch,
                },
            )
            result.closed += 1
            row_log.info("issue_work_reconciled", attempts=w.attempts)
        except Exception:
            row_log.warning("issue_work_reconcile_row_failed")
            continue

    return result


def _cutoff_ts(days_ago: int) -> str:
    dt = _utcnow() - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}"[:3] + "Z"


def cleanup_events(data_dir: str, task_ids: list[str]) -> int:
    events_dir = Path(data_dir) / "events"
    if not events_dir.is_dir():
        return 0

    removed = 0
    for task_id in task_ids:
        task_events_dir = events_dir / task_id
        if task_events_dir.is_dir():
            shutil.rmtree(task_events_dir, ignore_errors=True)
            removed += 1
    return removed


def cleanup_orphan_workspaces(data_dir: str, active_ids: set[str]) -> int:
    workspaces_dir = Path(data_dir) / "workspaces"
    if not workspaces_dir.is_dir():
        return 0

    removed = 0
    for child in workspaces_dir.iterdir():
        if child.is_dir() and child.name not in active_ids:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


def cleanup_database_records(
    store: SQLiteStore, events_days: int, db_days: int
) -> dict[str, int]:
    events_cutoff = _cutoff_ts(events_days)
    completed_ids = store.find_completed_task_ids_older_than(events_cutoff)

    db_counts: dict[str, int] = {}
    if not completed_ids:
        db_counts.update(
            task_events=0,
            tasks=0,
            processed_deliveries=0,
            daily_budget=0,
        )
    else:
        db_cutoff = _cutoff_ts(db_days)
        db_counts = store.delete_old_records(completed_ids, db_cutoff)

    return {
        "task_events": int(db_counts.get("task_events", 0)),
        "tasks": int(db_counts.get("tasks", 0)),
        "processed_deliveries": int(db_counts.get("processed_deliveries", 0)),
        "daily_budget": int(db_counts.get("daily_budget", 0)),
    }


def run_maintenance(store: SQLiteStore, settings: Settings) -> MaintenanceResult:
    log = bind_context(logger)
    try:
        result = MaintenanceResult()

        events_cutoff = _cutoff_ts(settings.events_retention_days)
        completed_ids = store.find_completed_task_ids_older_than(events_cutoff)
        result.events_removed = cleanup_events(settings.data_dir, completed_ids)

        active_ids = store.find_running_task_ids()
        result.workspaces_removed = cleanup_orphan_workspaces(
            settings.data_dir, active_ids
        )

        result.audit_files_pruned = len(
            prune_old_audit_files(
                settings.data_dir, max_days=settings.audit_retention_days
            )
        )

        db_counts = cleanup_database_records(
            store, settings.events_retention_days, settings.db_retention_days
        )
        result.db_task_events = db_counts["task_events"]
        result.db_tasks = db_counts["tasks"]
        result.db_deliveries = db_counts["processed_deliveries"]
        result.db_budget = db_counts["daily_budget"]

        log.info(
            "maintenance_complete",
            events_removed=result.events_removed,
            workspaces_removed=result.workspaces_removed,
            audit_files_pruned=result.audit_files_pruned,
            db_task_events=result.db_task_events,
            db_tasks=result.db_tasks,
            db_deliveries=result.db_deliveries,
            db_budget=result.db_budget,
            total=result.total,
        )

        if result.total > 0:
            write_audit_entry(
                settings.data_dir,
                {
                    "event": "maintenance_cleanup",
                    "events_removed": result.events_removed,
                    "workspaces_removed": result.workspaces_removed,
                    "audit_files_pruned": result.audit_files_pruned,
                    "db_task_events": result.db_task_events,
                    "db_tasks": result.db_tasks,
                    "db_deliveries": result.db_deliveries,
                    "db_budget": result.db_budget,
                    "total": result.total,
                    "events_retention_days": settings.events_retention_days,
                    "db_retention_days": settings.db_retention_days,
                    "audit_retention_days": settings.audit_retention_days,
                },
                level="info",
            )

        return result
    except Exception:
        log.exception("maintenance_failed")
        return MaintenanceResult()
