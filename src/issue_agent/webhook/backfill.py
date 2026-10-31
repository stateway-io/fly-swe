"""Re-normalize legacy tasks into ``trigger_json`` (PRD §3.6, migration v6).

Rows enqueued before the canonical model existed only carry the raw GitHub
payload. The store can't normalize them itself — that would make ``queue/``
import the GitHub parser — so both processes call this right after
``store.init()``. It's idempotent: once every row has a trigger it's a
single empty query.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import ValidationError

from issue_agent.core.models import Ignored, TriggerEvent, TriggerKind
from issue_agent.logging import bind_context, get_logger
from issue_agent.providers.github.normalize import bot_identity_from_settings
from issue_agent.providers.github.settings import GitHubProviderSettings
from issue_agent.providers.github.webhook import normalize_payload
from issue_agent.queue.models import TaskStatus

if TYPE_CHECKING:
    from issue_agent.config import Settings
    from issue_agent.queue.models import Task
    from issue_agent.queue.store import SQLiteStore

logger = get_logger(__name__)

# Every row that predates trigger_json came from the single GitHub App.
LEGACY_PROVIDER_INSTANCE = "github"

_EVENT_FOR_KIND = {
    TriggerKind.COMMENT_COMMAND: "issue_comment",
}

# A task in one of these states may still run (or be retried), so it can't
# be left without a trigger.
_LIVE_STATUSES = frozenset(
    {TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.FAILED_RECOVERABLE}
)


def _renormalize(
    task: Task, settings: Settings, config: GitHubProviderSettings
) -> TriggerEvent | None:
    try:
        payload = json.loads(task.payload_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        result = normalize_payload(
            task.delivery_id,
            _EVENT_FOR_KIND.get(task.trigger_kind, "issues"),
            payload,
            instance_id=LEGACY_PROVIDER_INSTANCE,
            # The adapter doesn't filter, so a task accepted under an older
            # config is never retroactively dropped.
            identity=bot_identity_from_settings(config, settings.agent_name),
        )
    except ValidationError:
        return None
    if isinstance(result, Ignored) or result.kind is not task.trigger_kind:
        return None
    return result


def backfill_trigger_json(store: SQLiteStore, settings: Settings) -> int:
    """Fill ``trigger_json`` for every task that lacks it; returns how many
    were filled. A live task whose payload can't be normalized fails
    terminally instead of crashing every consumer downstream."""
    pending = store.find_tasks_without_trigger()
    if not pending:
        return 0
    config = settings.providers.get(LEGACY_PROVIDER_INSTANCE)
    if not isinstance(config, GitHubProviderSettings):
        # The rows can only be read as GitHub payloads of the `github`
        # instance; the worker's payload guard fails them instead.
        bind_context(logger).warning(
            "backfill_skipped_no_github_instance", pending=len(pending)
        )
        return 0
    filled = 0
    for task in pending:
        log = bind_context(logger, task_id=task.id, repo=task.repo)
        trigger = _renormalize(task, settings, config)
        if trigger is None:
            if task.status in _LIVE_STATUSES:
                store.update_status(
                    task.id, TaskStatus.FAILED_TERMINAL, error="invalid_trigger"
                )
                log.warning("backfill_invalid_trigger", status=task.status.value)
            continue
        # The credentials hint comes from the payload, like at ingestion:
        # the legacy installation_id column is no longer read.
        store.set_task_trigger(
            task.id,
            trigger.model_dump_json(),
            LEGACY_PROVIDER_INSTANCE,
            trigger.credentials_ref,
        )
        filled += 1
    if filled:
        bind_context(logger).info("backfill_trigger_json", filled=filled)
    return filled
