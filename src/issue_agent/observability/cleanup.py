from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from issue_agent.logging import bind_context, get_logger
from issue_agent.observability.audit import prune_old_audit_files, write_audit_entry
from issue_agent.observability.prometheus_metrics import observe_issue_work_closed
from issue_agent.queue.models import TaskStatus
from issue_agent.queue.store import format_ts

if TYPE_CHECKING:
    from collections.abc import Callable

    from issue_agent.config import Settings
    from issue_agent.core.models import PRState
    from issue_agent.providers.registry import ProviderRegistry
    from issue_agent.queue.store import SQLiteStore

logger = get_logger(__name__)


TASK_RETENTION_CATEGORIES = ("pr_merged", "pr_closed", "aborted", "failed", "other")

# The age-based category of a finished task on an issue with no PR.
_STATUS_CATEGORY = {
    TaskStatus.SUCCEEDED.value: "other",
    TaskStatus.FAILED_TERMINAL.value: "failed",
    TaskStatus.ABORTED_BUDGET.value: "aborted",
    TaskStatus.ABORTED_POLICY.value: "aborted",
}

IssueKey = tuple[str, str, int]


@dataclass
class MaintenanceResult:
    events_removed: int = 0
    workspaces_removed: int = 0
    audit_files_pruned: int = 0
    db_task_events: int = 0
    db_tasks: int = 0
    db_deliveries: int = 0
    db_budget: int = 0
    db_issue_work: int = 0
    db_pr_states: int = 0
    prs_checked: int = 0
    # Task rows deleted, per retention.tasks category.
    db_tasks_by_category: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        """Everything removed (forge lookups don't count)."""
        return (
            self.events_removed
            + self.workspaces_removed
            + self.audit_files_pruned
            + self.db_task_events
            + self.db_tasks
            + self.db_deliveries
            + self.db_budget
            + self.db_issue_work
            + self.db_pr_states
        )


@dataclass
class RetentionSelection:
    """What the per-category policy says to delete this run."""

    task_ids: dict[str, list[str]] = field(default_factory=dict)
    # Issues whose PR rule fired: their finished issue_work rows go too.
    issue_work_keys: list[IssueKey] = field(default_factory=list)

    @property
    def all_task_ids(self) -> list[str]:
        return [tid for ids in self.task_ids.values() for tid in ids]


@dataclass
class ReconcileResult:
    checked: int = 0
    closed: int = 0


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _default_issue_state_fn(
    registry: ProviderRegistry,
) -> Callable[[str, str, int], str]:
    """Build the real "is this issue closed on the forge?" probe: each
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
    """Close every open ``issue_work`` whose issue is already closed on the forge.
    This is the safety net for a missed ``issues.closed`` webhook or a
    service outage — not the primary path, which is the zero-cost
    handler. Bounded by ``resume_reconcile_max_per_cycle`` forge lookups per
    call; a per-row failure is logged and the sweep continues. Like
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


def task_retention_policy(settings: Settings) -> dict[str, int | None]:
    """retention.tasks.* by category: days, or None for "forever"."""
    return {
        "pr_merged": settings.retention_tasks_pr_merged,
        "pr_closed": settings.retention_tasks_pr_closed,
        "aborted": settings.retention_tasks_aborted,
        "failed": settings.retention_tasks_failed,
        "other": settings.retention_tasks_other,
    }


def _default_pr_state_fn(registry: ProviderRegistry) -> Callable[[str, str, int], PRState]:
    """The real PR-state probe, on the provider instance that recorded the
    PR. An instance no longer configured (``KeyError``) or without a PR API
    (``LookupError``) fails only that row — which then stays unpruned."""

    def _state(provider_instance: str, repo: str, number: int) -> PRState:
        pulls = registry.get(provider_instance).pulls(repo)
        if pulls is None:
            raise LookupError(f"provider instance {provider_instance!r} has no PR API")
        return pulls.get_pr_state(number)

    return _state


def reconcile_pr_states(
    store: SQLiteStore,
    settings: Settings,
    registry: ProviderRegistry,
    *,
    pr_state_fn: Callable[[str, str, int], PRState] | None = None,
) -> int:
    """Refresh the ``pr_state`` cache for PRs never checked or last seen
    open, at most ``retention_pr_check_max_per_cycle`` forge lookups. A
    no-op when both PR rules are "forever" — nothing would read the result.
    Best-effort: a row's failure is logged and the sweep goes on; it never
    propagates. Returns how many PRs were checked."""
    if settings.retention_tasks_pr_merged is None and settings.retention_tasks_pr_closed is None:
        return 0
    try:
        prs = store.list_prs_to_check(settings.retention_pr_check_max_per_cycle)
        if not prs:
            return 0
        state_fn = pr_state_fn or _default_pr_state_fn(registry)
    except Exception:
        bind_context(logger).exception("pr_state_reconcile_setup_failed")
        return 0

    checked = 0
    for provider_instance, repo, number in prs:
        try:
            pr = state_fn(provider_instance, repo, number)
            closed_at = format_ts(pr.closed_at) if pr.closed_at is not None else None
            store.upsert_pr_state(provider_instance, repo, number, pr.state, closed_at)
            checked += 1
        except Exception:
            bind_context(
                logger, provider_instance=provider_instance, repo=repo, pr_number=number
            ).warning("pr_state_check_failed")
    return checked


def _pr_verdicts(
    store: SQLiteStore,
) -> dict[IssueKey, tuple[str, str | None] | None]:
    """Per issue with any PR: ``None`` while one of its PRs is open or
    unchecked, else ``(category, clock)`` — ``pr_merged`` if any PR was
    merged, ``pr_closed`` otherwise, clocked from the latest close."""
    live: set[IssueKey] = set()
    merged: set[IssueKey] = set()
    clocks: dict[IssueKey, str | None] = {}
    for key, state, closed_at in store.list_issue_pr_states():
        if state not in ("merged", "closed"):
            live.add(key)
            continue
        if state == "merged":
            merged.add(key)
        latest = clocks.get(key)
        clocks[key] = max(closed_at, latest) if closed_at and latest else closed_at or latest
    verdicts: dict[IssueKey, tuple[str, str | None] | None] = dict.fromkeys(live)
    for key, clock in clocks.items():
        if key not in live:
            verdicts[key] = ("pr_merged" if key in merged else "pr_closed", clock)
    return verdicts


def select_for_retention(store: SQLiteStore, policy: dict[str, int | None]) -> RetentionSelection:
    """Apply the per-category policy. An issue still in play (a task that
    may run, or open work) keeps everything. An issue with a PR follows only
    the PR rules — kept whole while any PR is open, then deleted whole once
    its rule's period has passed since the close. Other finished tasks age
    out by status from ``completed_at``."""
    cutoffs = {cat: _cutoff_ts(days) for cat, days in policy.items() if days is not None}
    selection = RetentionSelection(task_ids={cat: [] for cat in TASK_RETENTION_CATEGORIES})
    busy = store.list_busy_issue_keys()
    verdicts = _pr_verdicts(store)

    due_pr_issues: set[IssueKey] = set()
    for key, verdict in verdicts.items():
        if key in busy or verdict is None:
            continue
        category, clock = verdict
        cutoff = cutoffs.get(category)
        if cutoff is not None and clock is not None and clock <= cutoff:
            due_pr_issues.add(key)
    selection.issue_work_keys = sorted(due_pr_issues)

    for task in store.list_terminal_tasks():
        key = (task["provider_instance"], task["repo"], task["issue_number"])
        if key in busy:
            continue
        if key in verdicts:
            verdict = verdicts[key]
            if key in due_pr_issues and verdict is not None:
                selection.task_ids[verdict[0]].append(task["id"])
            continue
        category = _STATUS_CATEGORY[task["status"]]
        cutoff = cutoffs.get(category)
        if cutoff is not None and task["completed_at"] <= cutoff:
            selection.task_ids[category].append(task["id"])
    return selection


def cleanup_database_records(
    store: SQLiteStore, settings: Settings
) -> tuple[MaintenanceResult, list[str]]:
    """Delete the rows the retention policy selects; returns the counts and
    the deleted task ids (whose trajectory dirs go too)."""
    result = MaintenanceResult()
    selection = select_for_retention(store, task_retention_policy(settings))
    deleted_ids = selection.all_task_ids

    counts = store.delete_tasks(deleted_ids)
    result.db_tasks = counts["tasks"]
    result.db_task_events = counts["task_events"]
    result.db_tasks_by_category = {cat: len(ids) for cat, ids in selection.task_ids.items()}
    result.db_issue_work = store.delete_finished_issue_work(selection.issue_work_keys)
    result.db_pr_states = store.delete_orphan_pr_states()

    pruned = store.prune_deliveries_and_budget(_cutoff_ts(settings.db_retention_days))
    result.db_deliveries = pruned["processed_deliveries"]
    result.db_budget = pruned["daily_budget"]
    return result, deleted_ids


def _period_label(days: int | None) -> int | str:
    return "forever" if days is None else days


def run_maintenance(
    store: SQLiteStore,
    settings: Settings,
    registry: ProviderRegistry | None = None,
) -> MaintenanceResult:
    """Prune on-disk and DB data per ``retention:``. With a ``registry``,
    first refresh forge PR states so the PR rules see merges/closes."""
    log = bind_context(logger)
    try:
        prs_checked = reconcile_pr_states(store, settings, registry) if registry else 0

        result, deleted_ids = cleanup_database_records(store, settings)
        result.prs_checked = prs_checked

        events_cutoff = _cutoff_ts(settings.events_retention_days)
        aged_ids = store.find_completed_task_ids_older_than(events_cutoff)
        result.events_removed = cleanup_events(
            settings.data_dir, sorted(set(aged_ids) | set(deleted_ids))
        )

        active_ids = store.find_running_task_ids()
        result.workspaces_removed = cleanup_orphan_workspaces(settings.data_dir, active_ids)

        result.audit_files_pruned = len(
            prune_old_audit_files(settings.data_dir, max_days=settings.audit_retention_days)
        )

        fields = {
            "events_removed": result.events_removed,
            "workspaces_removed": result.workspaces_removed,
            "audit_files_pruned": result.audit_files_pruned,
            "db_task_events": result.db_task_events,
            "db_tasks": result.db_tasks,
            "db_tasks_by_category": result.db_tasks_by_category,
            "db_deliveries": result.db_deliveries,
            "db_budget": result.db_budget,
            "db_issue_work": result.db_issue_work,
            "db_pr_states": result.db_pr_states,
            "prs_checked": result.prs_checked,
            "total": result.total,
        }
        log.info("maintenance_complete", **fields)

        if result.total > 0:
            write_audit_entry(
                settings.data_dir,
                {
                    "event": "maintenance_cleanup",
                    **fields,
                    "events_retention_days": settings.events_retention_days,
                    "db_retention_days": settings.db_retention_days,
                    "audit_retention_days": settings.audit_retention_days,
                    "task_retention": {
                        cat: _period_label(days)
                        for cat, days in task_retention_policy(settings).items()
                    },
                },
                level="info",
            )

        return result
    except Exception:
        log.exception("maintenance_failed")
        return MaintenanceResult()
