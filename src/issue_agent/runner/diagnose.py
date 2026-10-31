from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from issue_agent.diagnose.builder import build_diagnosis_comment
from issue_agent.diagnose.verdict import consume_sentinel, parse_verdict
from issue_agent.logging import bind_context, get_logger
from issue_agent.observability.audit import write_audit_entry
from issue_agent.observability.prometheus_metrics import observe_task_result
from issue_agent.queue.models import TaskStatus
from issue_agent.runner.prompts import load_diagnose_system_prompt
from issue_agent.runner.workspace import Workspace

if TYPE_CHECKING:
    from issue_agent.config import Settings
    from issue_agent.providers.base import ProviderPlugin
    from issue_agent.queue.models import Task
    from issue_agent.queue.store import SQLiteStore

logger = get_logger(__name__)


def _fail(
    store: SQLiteStore,
    task: Task,
    settings: Settings,
    status: TaskStatus,
    error: str,
    duration: float,
) -> None:
    store.append_task_event(
        task.id,
        "state_change",
        {"from": "running", "to": status.value, "action": "diagnose", "error": error},
    )
    store.update_status(task.id, status, error=error)
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
            "outcome": status.value,
            "action": "diagnose",
            "error": error,
        },
        level="error",
    )
    observe_task_result(
        "failed", duration, 0.0, 0, task.target_repo,
        provider_instance=task.provider_instance,
    )


def run_diagnosis(
    store: SQLiteStore, task: Task, settings: Settings, plugin: ProviderPlugin
) -> None:
    """DIAGNOSE: run the coding agent read-only against the repo and post a
    verdict on whether the issue's label matches reality. Deliberately does
    NOT reuse executor.run_task's FIX machinery — no issue_work (nothing to
    resume, no branch), no initial/progress comments, no git bookkeeping, no
    PR. The only public output is the sanitized, harness-built comment from
    diagnose/builder.py — the agent's own free-form submission is never
    read, let alone published (see diagnose/verdict.py, diagnose/builder.py).
    """
    _start = time.monotonic()
    task_log = bind_context(
        logger, task_id=task.id, issue_number=task.issue_number, repo=task.repo
    )

    route = settings.route_for(task.provider_instance, task.repo)
    if route is None:
        error = (
            f"no repo_routes entry for issue repo {task.repo!r} — the config "
            "must have changed between task enqueue and execution"
        )
        task_log.error("diagnose_route_not_found", error=error)
        _fail(
            store, task, settings, TaskStatus.FAILED_RECOVERABLE,
            error, time.monotonic() - _start,
        )
        return

    ws = Workspace(task.id, task.target_repo, settings, plugin.transport())

    try:
        ws_path = ws.clone(target_branch=task.base_branch)
    except Exception as exc:
        task_log.exception("diagnose_clone_failed")
        _fail(
            store, task, settings, TaskStatus.FAILED_RECOVERABLE,
            f"clone_failed: {exc}", time.monotonic() - _start,
        )
        return

    try:
        from issue_agent.runner.executor import MiniSWERunner, _fetch_comment_history

        result = MiniSWERunner().run(
            str(ws_path), task, settings, route,
            system_template=load_diagnose_system_prompt(),
            comment_history=_fetch_comment_history(
                task, settings, plugin.client(task.repo)
            ),
            terms=plugin.capabilities.terms,
        )

        raw = consume_sentinel(ws_path)
        verdict = parse_verdict(raw)

        comment = build_diagnosis_comment(verdict, task, settings.agent_name)
        comment_failed = False
        try:
            plugin.client(task.repo).post_comment(task.issue_number, comment)
        except Exception:
            task_log.warning("diagnose_comment_failed")
            comment_failed = True

        duration = time.monotonic() - _start
        store.update_status(
            task.id,
            TaskStatus.SUCCEEDED,
            cost_usd=result.cost_usd,
            iterations=result.iterations,
        )
        store.append_task_event(
            task.id,
            "state_change",
            {
                "from": "running",
                "to": "succeeded",
                "action": "diagnose",
                "actual_category": verdict.actual_category if verdict else None,
                "agent_error": result.error,
            },
        )
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
                "outcome": "succeeded_with_comment_error" if comment_failed else "succeeded",
                "action": "diagnose",
                "actual_category": verdict.actual_category if verdict else None,
                "confidence": verdict.confidence if verdict else None,
                "cost_usd": result.cost_usd,
                "iterations": result.iterations,
            },
        )
        if result.cost_usd > 0:
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            store.add_daily_budget(today, task.target_repo, result.cost_usd)
        observe_task_result(
            "succeeded", duration, result.cost_usd, result.iterations, task.target_repo,
            provider_instance=task.provider_instance,
        )
        task_log.info(
            "diagnose_completed",
            verdict_found=verdict is not None,
            agent_success=result.success,
        )
    finally:
        ws.cleanup()
