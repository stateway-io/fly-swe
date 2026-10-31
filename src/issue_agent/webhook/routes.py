from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from issue_agent.config import get_settings
from issue_agent.core.models import Ignored
from issue_agent.logging import bind_context, get_logger
from issue_agent.observability.audit import write_audit_entry
from issue_agent.observability.prometheus_metrics import (
    observe_issue_work_closed,
    set_queue_depth,
)
from issue_agent.policy.ingestion import ingestion_filter
from issue_agent.policy.label_router import get_label_category
from issue_agent.providers.base import WebhookRejectedError
from issue_agent.queue.models import Task, TriggerKind

if TYPE_CHECKING:
    from issue_agent.config import Settings
    from issue_agent.core.models import TriggerEvent
    from issue_agent.providers.registry import ProviderRegistry
    from issue_agent.queue.store import SQLiteStore

router = APIRouter()
logger = get_logger(__name__)


def _get_store(request: Request) -> SQLiteStore:
    return request.app.state.store


_LIFECYCLE_TRIGGERS = (TriggerKind.ISSUE_CLOSED, TriggerKind.ISSUE_REOPENED)
_CREATION_TRIGGER_KINDS = (TriggerKind.ISSUE_OPENED, TriggerKind.LABEL_ADDED)

# Log event per ``Ignored.reason`` — kept from when each filter lived inline
# in the handler, so dashboards/alerts keyed on them keep working.
_IGNORED_LOG_EVENTS = {
    "unhandled_event_action": "unhandled_event_action",
    "comment_on_pull_request": "ignored_pull_request_comment",
    "own_bot_comment": "ignored_own_comment",
    "issue_closed": "ignored_closed_issue_comment",
    "comment_author_not_allowed": "ignored_comment_author_not_allowed",
    "no_trigger_label": "ignored_no_trigger_label",
    "unsupported_event": "unsupported_event",
}


def _handle_issue_lifecycle(
    store: SQLiteStore, settings: Settings, trigger: TriggerEvent
) -> tuple[str, str]:
    """React to ``issues.closed`` / ``issues.reopened`` without enqueuing a
    Task (R7.1, R7.2) — zero GitHub API calls, zero LLM.

    ``closed`` closes an open ``issue_work`` row with reason ``issue_closed``;
    reopening never restarts paid work (§3.3). Every close/drop here leaves
    the same audit + metric trail the executor-driven closes do (R8.1/R8.3):
    ``issues.closed`` is the primary close path (D6), so it can't be the one
    the ``issue_work_attempts`` histogram and the audit log never see.
    Returns ``(status, reason)`` for the response.
    """
    instance = trigger.issue.provider_instance
    repo, issue_number = trigger.issue.repo, trigger.issue.number
    work = store.get_issue_work(instance, repo, issue_number)

    if trigger.kind is TriggerKind.ISSUE_REOPENED:
        if work is None:
            return "ignored", "issue_reopened"
        # A row closed with ``issue_closed`` makes the executor's R7.6 guard
        # abort every future run on this issue. Reopening the issue means
        # that close is no longer true: drop the spent row so a later
        # comment/label starts fresh work instead of being aborted (§3.3).
        if work.state == "closed" and work.closed_reason == "issue_closed":
            store.delete_issue_work(instance, repo, issue_number)
            write_audit_entry(
                settings.data_dir,
                {
                    "event": "issue_work_dropped_on_reopen",
                    "issue_repo": repo,
                    "provider_instance": instance,
                    "issue_number": issue_number,
                    "attempts": work.attempts,
                    "work_branch": work.work_branch,
                },
            )
            return "ignored", "issue_reopened"
        store.touch_issue_work(instance, repo, issue_number)
        return "ignored", "issue_reopened"

    # ISSUE_CLOSED
    if work is not None and work.state == "open":
        store.close_issue_work(instance, repo, issue_number, "issue_closed")
        observe_issue_work_closed(
            work.target_repo, work.attempts, provider_instance=instance
        )
        write_audit_entry(
            settings.data_dir,
            {
                "event": "issue_work_closed",
                "issue_repo": repo,
                "provider_instance": instance,
                "issue_number": issue_number,
                "closed_reason": "issue_closed",
                "attempts": work.attempts,
                "work_branch": work.work_branch,
            },
        )
        return "handled", "issue_work_closed"
    return "ignored", "no_open_work"


def _get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.registry


@router.post("/webhooks/{instance_id}")
async def handle_webhook(instance_id: str, request: Request) -> Response:
    """Webhook dispatcher (PRD §3.4): the provider instance comes from the
    path — never from sniffing headers, since Gitea/Forgejo also send
    ``X-GitHub-*``. The GitHub App's URL stays ``/webhooks/github``."""
    settings = get_settings()
    store = _get_store(request)

    try:
        adapter = _get_registry(request).get(instance_id).webhook
    except KeyError:
        bind_context(logger, instance_id=instance_id).warning("unknown_provider_instance")
        return JSONResponse(
            status_code=404,
            content={"error": "unknown provider instance"},
        )

    body = await request.body()
    headers = request.headers
    event = adapter.event_name(headers)

    try:
        delivery_id = adapter.delivery_key(headers, body)
    except WebhookRejectedError as rejected:
        # Uvicorn's access line is all the log would otherwise show for a
        # 400, so every rejection below says why.
        logger.warning(rejected.log_event)
        return JSONResponse(
            status_code=rejected.status,
            content={"error": rejected.error},
        )

    try:
        # Authenticate before looking at anything else: an unsigned request
        # must never get a 2xx, whatever event it claims to be.
        adapter.verify_signature(headers, body)
    except WebhookRejectedError as rejected:
        # Never recorded as processed: an unauthenticated request must not
        # be able to burn a real delivery id.
        bind_context(logger, delivery_id=delivery_id, event=event).warning(
            rejected.log_event
        )
        return JSONResponse(
            status_code=rejected.status,
            content={"error": rejected.error},
        )

    if store.is_delivery_processed(instance_id, delivery_id):
        log = bind_context(logger, delivery_id=delivery_id, event=event)
        log.info("duplicate_delivery_ignored")
        return JSONResponse(
            status_code=200,
            content={"status": "duplicate", "delivery_id": delivery_id},
        )

    try:
        parsed = adapter.parse_event(headers, body)
    except WebhookRejectedError as rejected:
        if rejected.mark_reason is not None:
            store.mark_delivery_processed(instance_id, delivery_id, event, rejected.mark_reason)
        bind_context(logger, delivery_id=delivery_id, event=event).warning(
            rejected.log_event
        )
        return JSONResponse(
            status_code=rejected.status,
            content={"error": rejected.error},
        )
    action = parsed.action
    result = parsed.result

    if isinstance(result, Ignored) and result.repo is None:
        # Nothing to route (e.g. an event type the agent doesn't subscribe
        # to): ack it so the forge doesn't record a failed delivery.
        store.mark_delivery_processed(instance_id, delivery_id, event, action)
        bind_context(logger, delivery_id=delivery_id, event=event).info(
            _IGNORED_LOG_EVENTS.get(result.reason, "ignored_event")
        )
        return JSONResponse(
            status_code=200,
            content={"status": "ignored", "reason": result.reason},
        )

    repo = result.repo if isinstance(result, Ignored) else result.issue.repo
    route = settings.route_for(instance_id, repo)
    if route is None:
        store.mark_delivery_processed(instance_id, delivery_id, event, action)
        log = bind_context(logger, delivery_id=delivery_id, repo=repo)
        log.warning("repo_not_allowed")
        return JSONResponse(
            status_code=200,
            content={"status": "ignored", "reason": "repo_not_allowed"},
        )

    if isinstance(result, Ignored):
        reason: str | None = result.reason
    else:
        trigger = result
        reason = ingestion_filter(trigger, settings)
    if reason is not None:
        # No network call follows on this path — safe to mark immediately.
        store.mark_delivery_processed(instance_id, delivery_id, event, action)
        bind_context(
            logger, delivery_id=delivery_id, event=event, action=action
        ).info(_IGNORED_LOG_EVENTS.get(reason, "ignored_event"))
        return JSONResponse(
            status_code=200,
            content={"status": "ignored", "reason": reason},
        )

    if trigger.kind in _LIFECYCLE_TRIGGERS:
        # No network call follows on this path — safe to mark immediately.
        status, reason = _handle_issue_lifecycle(store, settings, trigger)
        store.mark_delivery_processed(instance_id, delivery_id, event, action)
        bind_context(
            logger,
            delivery_id=delivery_id,
            issue_number=trigger.issue.number,
            repo=repo,
        ).info("issue_lifecycle_handled", action=action, reason=reason)
        return JSONResponse(
            status_code=200,
            content={"status": status, "reason": reason},
        )

    label_category = get_label_category(trigger.issue.labels)

    task = Task(
        delivery_id=delivery_id,
        repo=repo,
        issue_number=trigger.issue.number,
        trigger_kind=trigger.kind,
        label_category=label_category,
        target_repo=route.target_repo,
        base_branch=route.base_branch,
        workflow=route.workflow,
        trigger_json=trigger.model_dump_json(),
        provider_instance=trigger.issue.provider_instance,
        credentials_ref=trigger.credentials_ref,
    )

    if trigger.kind in _CREATION_TRIGGER_KINDS:
        # GitHub fires both `issues.opened` and `issues.labeled` — in no
        # guaranteed order — when an issue is created with labels already
        # attached. Only one of the two should become a task; a second one
        # just duplicates work and, after the first aborts, looks like an
        # unsolicited resume.
        # insert_task_unless_active_creation_task() makes the
        # check-then-insert atomic, so the drop doesn't depend on which of
        # the two webhooks is processed first. It only counts other
        # issue_opened/label_added tasks: an unrelated comment-triggered
        # task already running on the same issue never blocks a genuinely
        # new creation-trigger event.
        task_id = store.insert_task_unless_active_creation_task(task)
        if task_id is None:
            store.mark_delivery_processed(instance_id, delivery_id, event, action)
            reason = (
                "redundant_label_event"
                if trigger.kind is TriggerKind.LABEL_ADDED
                else "redundant_issue_opened_event"
            )
            bind_context(
                logger,
                delivery_id=delivery_id,
                issue_number=trigger.issue.number,
                repo=repo,
            ).info("ignored_redundant_creation_event", reason=reason)
            return JSONResponse(
                status_code=200,
                content={"status": "ignored", "reason": reason},
            )
    else:
        task_id = store.insert_task(task)

    # Marked processed only now that the task actually exists: insert_task()
    # is the way this handler can fail after signature verification.
    # Marking any earlier — as used to happen unconditionally right after
    # the repo route lookup — meant a mid-flight exception here still left
    # the delivery marked "processed", so the forge's retry of the same
    # delivery would be treated as a duplicate and the issue would never
    # get a task.
    store.mark_delivery_processed(instance_id, delivery_id, event, action)

    set_queue_depth(store.get_queue_depth())

    log = bind_context(
        logger,
        delivery_id=delivery_id,
        task_id=task_id,
        issue_number=trigger.issue.number,
        repo=repo,
        trigger_kind=trigger.kind.value,
    )
    log.info("task_enqueued")

    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "delivery_id": delivery_id,
            "task_id": task_id,
        },
    )
