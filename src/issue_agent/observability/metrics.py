from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from issue_agent.config import Settings
    from issue_agent.queue.store import SQLiteStore


@dataclass
class DailyBudgetMetrics:
    date: str
    total_cost_usd: float
    task_count: int
    max_budget_usd: float
    remaining_usd: float
    is_exceeded: bool

    def to_dict(self) -> dict:
        return asdict(self)


def get_daily_budget_metrics(
    store: SQLiteStore, settings: Settings
) -> DailyBudgetMetrics:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    budget = store.get_daily_budget(today, "global")
    remaining = max(0.0, settings.max_daily_budget_usd - budget.total_cost_usd)
    return DailyBudgetMetrics(
        date=today,
        total_cost_usd=budget.total_cost_usd,
        task_count=budget.task_count,
        max_budget_usd=settings.max_daily_budget_usd,
        remaining_usd=remaining,
        is_exceeded=remaining <= 0,
    )
