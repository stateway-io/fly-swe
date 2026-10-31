from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class DelegationResult:
    success: bool
    cost_usd: float = 0.0
    iterations: int = 0
    files_modified: list[str] = field(default_factory=list)
    abort_reason: str | None = None
    raw_log_path: str | None = None


@runtime_checkable
class CodingAgentDelegator(Protocol):
    name: str

    def execute(
        self,
        workspace_path: str,
        task_instruction: str,
        *,
        max_budget_usd: float,
        max_iterations: int,
        timeout_seconds: int,
    ) -> DelegationResult: ...
