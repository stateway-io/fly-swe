from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ValidationError

from issue_agent.core.models import TriggerEvent, TriggerKind

__all__ = [
    "CIWatch",
    "DailyBudget",
    "DeliveryRecord",
    "IssueWork",
    "Task",
    "TaskEvent",
    "TaskStatus",
    "TriggerKind",
]


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED_RECOVERABLE = "failed_recoverable"
    FAILED_TERMINAL = "failed_terminal"
    ABORTED_BUDGET = "aborted_budget"
    ABORTED_POLICY = "aborted_policy"


class Task(BaseModel):
    id: str = ""
    delivery_id: str
    repo: str
    issue_number: int
    trigger_kind: TriggerKind
    label_category: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    pr_number: int | None = None
    cost_usd: float = 0.0
    iterations: int = 0
    retry_count: int = 0
    error: str | None = None
    created_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    heartbeat_at: str | None = None
    target_repo: str = ""
    base_branch: str = "main"
    workflow: str = "trunk_based"
    # The canonical `TriggerEvent`, serialized at ingestion.
    trigger_json: str | None = None
    provider_instance: str = "github"
    credentials_ref: str | None = None

    @property
    def trigger(self) -> TriggerEvent | None:
        """The event that created this task; None when `trigger_json` is
        missing or doesn't validate (the worker's payload guard rejects
        such tasks before anything else reads it)."""
        if not self.trigger_json:
            return None
        try:
            return TriggerEvent.model_validate_json(self.trigger_json)
        except ValidationError:
            return None


class IssueWork(BaseModel):
    """Per-issue work state — one row per (issue_repo, issue_number),
    spanning as many tasks as the work takes.

    `tasks` stays the webhook-delivery queue; this is the business-state
    entity that answers "has the agent already worked this issue, on which
    branch, with what result". `work_branch` is frozen at open time and
    read from here on every subsequent run.
    """

    provider_instance: str = "github"
    issue_repo: str
    issue_number: int
    target_repo: str
    base_branch: str
    work_branch: str
    state: str = "open"  # open | closed
    closed_reason: str | None = None  # see PRD §3.3
    attempts: int = 0
    last_task_id: str | None = None
    last_error: str | None = None
    last_summary: str | None = None
    last_summary_kind: str = ""  # '' | mechanical | llm | submission
    pr_number: int | None = None
    # Cost/iterations summed across every attempt this work item has run,
    # so a resumed run's own (often tiny) per-attempt cost isn't mistaken
    # for the issue's total spend in GitHub comments.
    total_cost_usd: float = 0.0
    total_iterations: int = 0
    created_at: str = ""
    updated_at: str = ""


class CIWatch(BaseModel):
    """The CI monitor's watch on the head commit the agent last pushed to an
    issue's PR. One row per issue, replaced on every push; ``state`` is
    ``watching`` until the forge's CI on ``head_sha`` settles."""

    provider_instance: str
    issue_repo: str
    issue_number: int
    target_repo: str
    pr_number: int
    head_sha: str
    # watching | passed | fix_enqueued | exhausted | no_ci | timed_out | cancelled
    state: str = "watching"
    last_task_id: str | None = None
    fix_task_id: str | None = None
    created_at: str = ""
    last_polled_at: str | None = None


class TaskEvent(BaseModel):
    id: int | None = None
    task_id: str
    event_type: str
    payload_json: str = "{}"
    created_at: str = ""


class DeliveryRecord(BaseModel):
    delivery_id: str
    received_at: str = ""
    event_type: str
    action: str | None = None


class DailyBudget(BaseModel):
    date: str
    scope: str = "global"
    total_cost_usd: float = 0.0
    task_count: int = 0
