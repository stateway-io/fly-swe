from __future__ import annotations

from pathlib import Path

from issue_agent.runner.delegators import DelegationResult


class FakeDelegator:
    """Deterministic delegator for testing the ``CodingAgentDelegator`` protocol.

    Satisfies both ``CodingAgentDelegator`` (the delegation protocol) and
    ``AgentRunner`` (current executor protocol) via duck-typing.

    Parameters
    ----------
    result:
        Fixed ``DelegationResult`` returned on every ``execute()`` call.
    write_file:
        If set, the delegator writes this file content into the workspace
        during ``execute()``, simulating a real agent edit.
    """

    name = "fake"

    def __init__(
        self,
        result: DelegationResult | None = None,
        *,
        write_file: tuple[str, str] | None = None,
    ) -> None:
        self._result = result or DelegationResult(success=True, cost_usd=0.1, iterations=3)
        self._write_file = write_file
        self.execute_count = 0

    def execute(
        self,
        workspace_path: str,
        task_instruction: str,
        *,
        max_budget_usd: float,
        max_iterations: int,
        timeout_seconds: int,
    ) -> DelegationResult:
        self.execute_count += 1
        if max_budget_usd <= 0:
            return DelegationResult(success=False, abort_reason="budget")
        if max_iterations <= 0:
            return DelegationResult(success=False, abort_reason="iterations")
        if timeout_seconds <= 0:
            return DelegationResult(success=False, abort_reason="timeout")
        if self._write_file:
            fpath, fcontent = self._write_file
            full = Path(workspace_path) / fpath
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(fcontent)
            return DelegationResult(
                success=True,
                cost_usd=self._result.cost_usd,
                iterations=self._result.iterations,
                files_modified=[fpath],
            )
        return self._result



