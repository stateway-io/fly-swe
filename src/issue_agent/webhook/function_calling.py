"""The advisory function-calling check, run off the web process's boot path.

Answering it needs LiteLLM, and importing LiteLLM costs seconds (several on a
shared 1-vCPU Machine) — long enough that, done inside the app's lifespan, it
delays the moment uvicorn opens its port, and with it every webhook the Fly
proxy is holding while the Machine boots. The check is informational only (it
never flips readiness), so nothing needs its answer before the app serves:
it runs once in a background thread and ``/readyz`` reports ``pending`` until
it is done.

``issue_agent.runner.llm_support`` is imported inside :meth:`run`, never at
module level, so importing the web app does not import LiteLLM.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from issue_agent.logging import get_logger

if TYPE_CHECKING:
    from issue_agent.config import Settings

logger = get_logger(__name__)

PENDING = "pending"


class FunctionCallingCheck:
    """Checks every distinct model this deployment might run — the global
    default plus any repo's ``llm.model`` override (``Settings.llm_profile``),
    so a repo pinned to a model without function-calling support isn't hidden
    behind a healthy global default.

    The models never change without a restart, so the answer is computed
    once and cached.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._result: str | None = None
        self._lock = threading.Lock()

    @property
    def result(self) -> str:
        """``ok``, ``warning: ...``, or ``pending`` while the check runs."""
        with self._lock:
            return self._result if self._result is not None else PENDING

    def start(self) -> threading.Thread:
        """Run the check in a daemon thread and return it."""
        thread = threading.Thread(target=self.run, name="function-calling-check", daemon=True)
        thread.start()
        return thread

    def run(self) -> None:
        try:
            result = self._check()
        except Exception as exc:  # advisory: never let it take the app down
            logger.warning("llm_function_calling_check_failed", error=str(exc))
            result = f"warning: could not check function-calling support ({exc})"
        with self._lock:
            self._result = result

    def _check(self) -> str:
        from issue_agent.runner import llm_support

        settings = self._settings
        profiles = [settings.llm_profile(None)] + [
            settings.llm_profile(route) for route in settings.repo_routes
        ]
        seen: set[str] = set()
        warnings: list[str] = []
        for profile in profiles:
            if profile.model in seen:
                continue
            seen.add(profile.model)
            provider = llm_support.resolve_provider(profile.model, profile.provider)
            warning = llm_support.check_function_calling(profile.model, provider)
            if warning:
                logger.warning(
                    "llm_function_calling_unsupported",
                    model=profile.model,
                    provider=provider or "(derived)",
                    detail=warning,
                )
                warnings.append(f"{profile.model}: {warning}")
        return f"warning: {'; '.join(warnings)}" if warnings else "ok"
