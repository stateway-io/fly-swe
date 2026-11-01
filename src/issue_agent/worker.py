from __future__ import annotations

import contextlib
import signal
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from issue_agent.config import Settings, get_settings
from issue_agent.logging import bind_context, get_logger, setup_logging
from issue_agent.observability.alerts import send_budget_alert
from issue_agent.observability.audit import (
    prune_old_audit_files,
    write_audit_entry,
)
from issue_agent.observability.cleanup import reconcile_issue_work, run_maintenance
from issue_agent.observability.prometheus_metrics import (
    init_metrics,
    observe_task_result,
)
from issue_agent.policy.guards import (
    check_author_allowed,
    check_comment_author_allowed,
    check_daily_budget,
    check_repo_allowed,
    validate_task_payload,
)
from issue_agent.policy.label_router import LabelAction, classify_labels
from issue_agent.providers.registry import ProviderRegistry
from issue_agent.queue.models import Task, TaskStatus
from issue_agent.queue.store import SQLiteStore
from issue_agent.triage.builder import build_triage_comment

if TYPE_CHECKING:
    from issue_agent.providers.base import ProviderPlugin

logger = get_logger(__name__)

_RUNNING = True

# Tracks which (date, scope) pairs have already triggered a budget-exceeded
# alert, so a repo stuck against its daily cap doesn't spam the webhook once
# per blocked task — each scope still gets its own alert, independently.
_budget_alerted: dict[str, str] = {}


def _utcnow() -> str:
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond:06d}"[:3] + "Z"


def _threshold_ts(seconds_ago: int) -> str:
    ts = datetime.now(UTC).timestamp() - seconds_ago
    dt = datetime.fromtimestamp(ts, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}"[:3] + "Z"


def _compute_backoff(retry_count: int, schedule: list[int]) -> int:
    idx = max(retry_count, 0)
    return schedule[min(idx, len(schedule) - 1)]


def _signal_handler(signum: int, frame: Any | None) -> None:
    global _RUNNING
    _RUNNING = False
    sig_logger = get_logger("signal")
    sig_logger.info("shutdown_requested", signal=signal.Signals(signum).name)


def run_orphan_recovery(store: SQLiteStore, settings: Settings) -> int:
    threshold = _threshold_ts(settings.orphan_timeout_seconds)
    orphans = store.find_orphaned_tasks(threshold)
    if not orphans:
        return 0

    recovered = 0

    for task in orphans:
        task_log = bind_context(logger, task_id=task.id)

        if task.retry_count >= settings.max_task_retries:
            store.update_status(
                task.id,
                TaskStatus.FAILED_TERMINAL,
                error="exceeded_recovery_retries",
            )
            store.append_task_event(
                task.id,
                "orphaned_recovery",
                {
                    "action": "failed_terminal",
                    "reason": "exceeded_recovery_retries",
                    "last_heartbeat": task.heartbeat_at,
                    "retry_count": task.retry_count,
                },
            )
            write_audit_entry(
                settings.data_dir,
                {
                    "event": "orphan_recovery",
                    "task_id": task.id,
                    "provider_instance": task.provider_instance,
                    "action": "failed_terminal",
                    "reason": "exceeded_recovery_retries",
                    "retry_count": task.retry_count,
                },
                level="error",
            )
            task_log.warning(
                "orphan_recovery_exhausted",
                retry_count=task.retry_count,
                last_heartbeat=task.heartbeat_at,
            )
        else:
            workspace = Path(settings.data_dir) / "workspaces" / task.id
            if workspace.exists():
                import shutil

                shutil.rmtree(workspace, ignore_errors=True)

            try:
                store.reset_to_pending(task.id, task.retry_count + 1)
            except sqlite3.IntegrityError:
                task_log.warning(
                    "orphan_recovery_conflict",
                    repo=task.repo,
                    issue_number=task.issue_number,
                    trigger_kind=task.trigger_kind,
                )
                store.update_status(
                    task.id,
                    TaskStatus.FAILED_TERMINAL,
                    error="orphan_recovery_conflict",
                )
                store.append_task_event(
                    task.id,
                    "orphaned_recovery",
                    {
                        "action": "failed_terminal",
                        "reason": "conflict_already_pending",
                        "retry_count": task.retry_count,
                        "last_heartbeat": task.heartbeat_at,
                    },
                )
                write_audit_entry(
                    settings.data_dir,
                    {
                        "event": "orphan_recovery",
                        "task_id": task.id,
                        "provider_instance": task.provider_instance,
                        "action": "failed_terminal",
                        "reason": "conflict_already_pending",
                    },
                    level="error",
                )
                continue

            store.append_task_event(
                task.id,
                "orphaned_recovery",
                {
                    "action": "reset_to_pending",
                    "retry_count": task.retry_count + 1,
                    "last_heartbeat": task.heartbeat_at,
                },
            )
            write_audit_entry(
                settings.data_dir,
                {
                    "event": "orphan_recovery",
                    "task_id": task.id,
                    "provider_instance": task.provider_instance,
                    "action": "reset_to_pending",
                    "retry_count": task.retry_count + 1,
                },
                level="warn",
            )

            task_log.info(
                "orphan_recovered",
                retry_count=task.retry_count + 1,
            )
            recovered += 1

    if recovered:
        log = bind_context(logger)
        log.info(
            "orphan_recovery_complete",
            recovered=recovered,
            total=len(orphans),
        )

    return recovered


def _write_task_audit(
    settings: Settings,
    task: Task,
    outcome: str,
    level: str = "info",
    **extra: Any,
) -> None:
    write_audit_entry(
        settings.data_dir,
        {
            "event": "task_processed",
            "task_id": task.id,
            "provider_instance": task.provider_instance,
            "repo": task.repo,
            "issue_number": task.issue_number,
            "trigger_kind": task.trigger_kind.value,
            "label_category": task.label_category,
            "outcome": outcome,
            **extra,
        },
        level=level,
    )


_blocked_reasons: dict[str, str] = {
    "payload": "The webhook payload is invalid.",
    "repo": "The repository is not on the allowlist.",
    "author": "The issue author is not on the allowlist.",
    "budget": "The agent's daily budget has been exceeded.",
    "skip": "The issue was classified as out of scope by the label policy.",
}


def _build_blocked_comment(task: Task, reason: str, agent_name: str) -> str:
    return (
        f"🤖 **{agent_name}** — task not processed.\n\n"
        f"{reason}\n\n"
        f"Cost: US$ 0.00 | Iterations: 0\n\n"
        f"---\nTask: `{task.id}`"
    )


def _build_retry_exhausted_comment(
    task: Task, agent_name: str, resumable_branch: str | None = None
) -> str:
    resume_note = (
        f"The partial work is saved on branch `{resumable_branch}`; comment "
        f"on the issue to resume from it.\n\n"
        if resumable_branch
        else ""
    )
    return (
        f"🤖 **{agent_name}** — task cancelled after repeated failures.\n\n"
        f"The task was aborted after {task.retry_count} unsuccessful attempts. "
        f"Transient errors (GitHub 5xx, provider 429, network timeout) "
        f"persisted beyond the retry limit.\n\n"
        f"{resume_note}"
        f"Cost: US$ {task.cost_usd:.2f} | Iterations: {task.iterations}\n\n"
        f"---\nTask: `{task.id}` | _#{task.issue_number}_"
    )


def _post_blocked_comment(
    task: Task, settings: Settings, plugin: ProviderPlugin, guard: str, task_log: Any
) -> None:
    try:
        reason = _blocked_reasons.get(guard, "Motivo desconhecido.")
        comment = _build_blocked_comment(task, reason, settings.agent_name)
        plugin.client(task.repo).post_comment(task.issue_number, comment)
    except Exception:
        task_log.warning("blocked_comment_failed", guard=guard)


def run_retry_recovery(store: SQLiteStore, settings: Settings, registry: ProviderRegistry) -> int:
    tasks = store.find_recoverable_tasks()
    if not tasks:
        return 0

    recovered = 0
    now = datetime.now(UTC)

    for task in tasks:
        task_log = bind_context(logger, task_id=task.id)

        completed_at: datetime | None = None
        if task.completed_at:
            with contextlib.suppress(ValueError, AttributeError):
                completed_at = datetime.fromisoformat(task.completed_at.replace("Z", "+00:00"))

        if completed_at is None:
            continue

        elapsed = (now - completed_at).total_seconds()
        backoff = _compute_backoff(task.retry_count, settings.backoff_schedule_seconds)

        if elapsed < backoff:
            continue

        if task.retry_count >= settings.max_task_retries:
            store.update_status(
                task.id,
                TaskStatus.FAILED_TERMINAL,
                error="exhausted_retries",
            )
            store.append_task_event(
                task.id,
                "retry_exhausted",
                {
                    "action": "failed_terminal",
                    "reason": "exhausted_retries",
                    "retry_count": task.retry_count,
                    "elapsed_seconds": elapsed,
                    "backoff_seconds": backoff,
                },
            )
            _write_task_audit(
                settings,
                task,
                "failed_terminal",
                level="error",
                error="exhausted_retries",
                retry_count=task.retry_count,
                elapsed_seconds=elapsed,
                backoff_seconds=backoff,
            )
            task_log.warning(
                "retry_exhausted",
                retry_count=task.retry_count,
                elapsed_seconds=elapsed,
            )

            try:
                work = store.get_issue_work(task.provider_instance, task.repo, task.issue_number)
                resumable_branch = work.work_branch if work and work.state == "open" else None
                comment = _build_retry_exhausted_comment(
                    task, settings.agent_name, resumable_branch or None
                )
                plugin = registry.get(task.provider_instance)
                plugin.client(task.repo).post_comment(task.issue_number, comment)
            except Exception:
                task_log.warning("retry_exhausted_comment_failed")
        else:
            store.reset_to_pending(task.id, task.retry_count + 1)
            store.append_task_event(
                task.id,
                "retry_recovery",
                {
                    "action": "reset_to_pending",
                    "retry_count": task.retry_count + 1,
                    "elapsed_seconds": elapsed,
                    "backoff_seconds": backoff,
                },
            )
            _write_task_audit(
                settings,
                task,
                "retry_recovery",
                level="warn",
                retry_count=task.retry_count + 1,
                elapsed_seconds=elapsed,
                backoff_seconds=backoff,
            )
            task_log.info(
                "retry_recovered",
                retry_count=task.retry_count + 1,
                elapsed_seconds=elapsed,
            )
            recovered += 1

    if recovered:
        log = bind_context(logger)
        log.info(
            "retry_recovery_complete",
            recovered=recovered,
            total=len(tasks),
        )

    return recovered


def run_task(
    store: SQLiteStore, task: Task, settings: Settings, registry: ProviderRegistry
) -> None:
    task_log = bind_context(
        logger,
        task_id=task.id,
        issue_number=task.issue_number,
        repo=task.repo,
    )

    try:
        plugin = registry.get(task.provider_instance)
        store.append_task_event(
            task.id,
            "state_change",
            {"from": "pending", "to": "running"},
        )

        ok, msg = validate_task_payload(task)
        if not ok:
            store.update_status(task.id, TaskStatus.FAILED_TERMINAL, error=msg)
            store.append_task_event(
                task.id,
                "state_change",
                {"from": "running", "to": "failed_terminal", "reason": msg},
            )
            _write_task_audit(
                settings, task, "failed_terminal", level="warn", guard="payload", error=msg
            )
            task_log.warning("guard_blocked", guard="payload", reason=msg)
            _post_blocked_comment(task, settings, plugin, "payload", task_log)
            observe_task_result(
                "failed", 0.0, 0.0, 0, task.target_repo, provider_instance=task.provider_instance
            )
            return

        ok, msg = check_repo_allowed(task, settings)
        if not ok:
            store.update_status(task.id, TaskStatus.ABORTED_POLICY, error=msg)
            store.append_task_event(
                task.id,
                "state_change",
                {"from": "running", "to": "aborted_policy", "reason": msg},
            )
            _write_task_audit(
                settings, task, "aborted_policy", level="warn", guard="repo", error=msg
            )
            task_log.warning("guard_blocked", guard="repo", reason=msg)
            _post_blocked_comment(task, settings, plugin, "repo", task_log)
            observe_task_result(
                "aborted", 0.0, 0.0, 0, task.target_repo, provider_instance=task.provider_instance
            )
            return

        ok, msg = check_author_allowed(task, settings)
        if not ok:
            store.update_status(task.id, TaskStatus.ABORTED_POLICY, error=msg)
            store.append_task_event(
                task.id,
                "state_change",
                {"from": "running", "to": "aborted_policy", "reason": msg},
            )
            _write_task_audit(
                settings, task, "aborted_policy", level="warn", guard="author", error=msg
            )
            task_log.warning("guard_blocked", guard="author", reason=msg)
            _post_blocked_comment(task, settings, plugin, "author", task_log)
            observe_task_result(
                "aborted", 0.0, 0.0, 0, task.target_repo, provider_instance=task.provider_instance
            )
            return

        ok, msg = check_comment_author_allowed(
            task,
            settings,
            lambda login: plugin.client(task.repo).author_permission(login),
        )
        if not ok:
            store.update_status(task.id, TaskStatus.ABORTED_POLICY, error=msg)
            store.append_task_event(
                task.id,
                "state_change",
                {"from": "running", "to": "aborted_policy", "reason": msg},
            )
            _write_task_audit(
                settings, task, "aborted_policy", level="warn", guard="comment_author", error=msg
            )
            task_log.warning("guard_blocked", guard="comment_author", reason=msg)
            # No _post_blocked_comment: the ingestion filter this stands in
            # for drops silently (R2.3, D5), and so does this.
            observe_task_result(
                "aborted", 0.0, 0.0, 0, task.target_repo, provider_instance=task.provider_instance
            )
            return

        ok, msg = check_daily_budget(store, settings, scope=task.target_repo)
        if not ok:
            # Originally, the main loop checked the (global) budget *before*
            # ever calling fetch_next(), so a pending task just sat
            # untouched in the queue until the budget reset the next day.
            # Per-repo budgets dropped that proactive gate (correctly — the budget's
            # *scope* is task.target_repo, which isn't known before a task
            # is in hand) but that means fetch_next() now always hands us a
            # task, and this guard used to respond by aborting it
            # (ABORTED_BUDGET, a terminal status) instead of leaving it
            # queued. That turned "wait for tomorrow's reset" into "needs a
            # manual re-trigger forever." Requeue it instead: put it back to
            # pending (same retry_count — this isn't a task failure) and
            # sleep for one poll interval so this doesn't spin into a tight
            # fetch→check→requeue loop, mirroring the old gate's cadence.
            store.reset_to_pending(task.id, task.retry_count)
            store.append_task_event(
                task.id,
                "state_change",
                {
                    "from": "running",
                    "to": "pending",
                    "reason": msg,
                },
            )
            _write_task_audit(
                settings, task, "requeued_budget", level="warn", guard="budget", error=msg
            )
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            alert_key = f"{today}:{task.target_repo}"
            if alert_key not in _budget_alerted:
                budget = store.get_daily_budget(today, task.target_repo)
                send_budget_alert(
                    settings.alert_webhook_url,
                    "per_task_budget_exceeded",
                    spent_usd=budget.total_cost_usd,
                    limit_usd=settings.max_daily_budget_usd,
                    task_id=task.id,
                    issue_number=task.issue_number,
                    repo=task.repo,
                )
                _budget_alerted[alert_key] = today
            task_log.warning("guard_blocked_requeued", guard="budget", reason=msg)
            # No _post_blocked_comment here: the task isn't done — it'll
            # run again automatically once the budget allows it — so
            # commenting "task aborted" on the issue every poll cycle
            # until then would just spam it.
            time.sleep(settings.poll_interval_seconds)
            return

        # validate_task_payload() above guarantees the trigger.
        trigger = task.trigger
        assert trigger is not None
        action = classify_labels(
            trigger.issue.labels,
            triage_only_categories=frozenset(settings.policy_triage_only_categories),
            diagnose_categories=frozenset(settings.policy_diagnose_categories),
        )

        if action is LabelAction.SKIP:
            store.update_status(
                task.id,
                TaskStatus.ABORTED_POLICY,
                error="skipped_by_policy",
            )
            store.append_task_event(
                task.id,
                "state_change",
                {
                    "from": "running",
                    "to": "aborted_policy",
                    "action": action.value,
                },
            )
            _write_task_audit(
                settings,
                task,
                "aborted_policy",
                level="info",
                action=action.value,
            )
            task_log.info("task_skipped", action=action.value)
            _post_blocked_comment(task, settings, plugin, "skip", task_log)
            observe_task_result(
                "aborted", 0.0, 0.0, 0, task.target_repo, provider_instance=task.provider_instance
            )
            return

        elif action is LabelAction.TRIAGE_ONLY:
            try:
                # label_category is now resolved once, at webhook-ingestion
                # time (webhook/routes.py), via the single unified
                # get_label_category() — no need to recompute it here.
                category = task.label_category
                comment = build_triage_comment(category, trigger, settings.agent_name)

                store.update_status(task.id, TaskStatus.SUCCEEDED)
                store.append_task_event(
                    task.id,
                    "state_change",
                    {
                        "from": "running",
                        "to": "succeeded",
                        "action": action.value,
                        "category": category,
                    },
                )
                _write_task_audit(
                    settings,
                    task,
                    "succeeded",
                    level="info",
                    action=action.value,
                    category=category,
                )

                plugin.client(task.repo).post_comment(task.issue_number, comment)

                task_log.info(
                    "task_triage_commented",
                    action=action.value,
                    category=category,
                )
                observe_task_result(
                    "succeeded",
                    0.0,
                    0.0,
                    0,
                    task.target_repo,
                    provider_instance=task.provider_instance,
                )
            except Exception:
                task_log.exception("triage_comment_failed")
                store.append_task_event(
                    task.id,
                    "error",
                    {"error": "triage_comment_failed"},
                )
                _write_task_audit(
                    settings,
                    task,
                    "succeeded_with_comment_error",
                    level="warn",
                    action=action.value,
                    error="triage_comment_failed",
                )
                observe_task_result(
                    "succeeded",
                    0.0,
                    0.0,
                    0,
                    task.target_repo,
                    provider_instance=task.provider_instance,
                )

        elif action is LabelAction.DIAGNOSE:
            from issue_agent.runner.diagnose import run_diagnosis

            run_diagnosis(store, task, settings, plugin)

        elif action is LabelAction.FIX:
            from issue_agent.runner import executor

            executor.run_task(store, task, settings, plugin)

        else:  # pragma: no cover — exhaustiveness guard, see test_worker.py
            raise AssertionError(f"unhandled LabelAction: {action!r}")

    except Exception:
        task_log.exception("task_processing_failed")
        store.update_status(
            task.id,
            TaskStatus.FAILED_TERMINAL,
            error="unhandled_worker_exception",
        )
        store.append_task_event(
            task.id,
            "error",
            {"error": "unhandled_worker_exception"},
        )
        _write_task_audit(
            settings,
            task,
            "failed_terminal",
            level="error",
            error="unhandled_worker_exception",
        )
        observe_task_result(
            "failed", 0.0, 0.0, 0, task.target_repo, provider_instance=task.provider_instance
        )


def main() -> None:
    global _RUNNING
    _RUNNING = True

    settings = get_settings()
    setup_logging(settings.log_level, settings.agent_name)
    init_metrics(settings)

    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    db_path = str(data_dir / "issue_agent.db")
    store = SQLiteStore(db_path)
    store.init()
    registry = ProviderRegistry.from_settings(settings)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    log = bind_context(logger)
    log.info(
        "worker_started",
        db_path=db_path,
        poll_interval=settings.poll_interval_seconds,
        orphan_timeout=settings.orphan_timeout_seconds,
        max_task_retries=settings.max_task_retries,
    )

    pruned_count = len(
        prune_old_audit_files(settings.data_dir, max_days=settings.audit_retention_days)
    )
    if pruned_count:
        log.info("audit_pruned", files=pruned_count)

    recovered = run_orphan_recovery(store, settings)
    if recovered:
        log.info("orphan_recovery_done", recovered=recovered)

    run_maintenance(store, settings)
    reconcile_issue_work(store, settings, registry)

    _last_maintenance: float = time.monotonic()
    _last_reconcile: float = time.monotonic()

    # No proactive budget check happens here: before fetch_next() there is
    # no task in hand yet, so the budget's scope (task.target_repo) isn't
    # known. The guard inside run_task checks the budget for that specific
    # task's scope before any work (and thus any cost) can happen.
    while _RUNNING:
        try:
            now = time.monotonic()
            if now - _last_maintenance > settings.cleanup_interval_minutes * 60:
                run_maintenance(store, settings)
                _last_maintenance = now

            if now - _last_reconcile > settings.resume_reconcile_interval_minutes * 60:
                reconcile_issue_work(store, settings, registry)
                _last_reconcile = now

            run_retry_recovery(store, settings, registry)

            task = store.fetch_next()
            if task is not None:
                run_task(store, task, settings, registry)
            else:
                time.sleep(settings.poll_interval_seconds)
        except Exception:
            logger.exception("worker_loop_error")
            time.sleep(settings.poll_interval_seconds)

    store.close()
    log.info("worker_stopped")


if __name__ == "__main__":
    main()
