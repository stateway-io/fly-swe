"""LiteLLM model wrapper that trusts the provider-reported cost.

mini-swe-agent's ``LitellmModel._calculate_cost`` computes cost purely from
LiteLLM's static price map (``litellm.cost_calculator.completion_cost``). Models
that are not in that map (e.g. ``openrouter/deepseek/deepseek-v4-flash-*``) yield
``0.0``, which is then swallowed by ``cost_tracking="ignore_errors"`` — so every
task accumulates ``cost = 0.0`` and the per-task / daily budget guards never fire.

OpenRouter already returns the real cost in ``usage.cost`` (LiteLLM stores it on
``response._hidden_params``). This subclass reads that first and only falls back
to the static price map when the provider gives us nothing.

It also owns the retry budget and the wall-time deadline of every call (see
``CostAwareLitellmModel``): upstream, a request that gets no bytes back is retried
by two stacked layers that each wait out the per-request timeout, and the task's
wall-time limit is only checked between steps.
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING

import litellm
from minisweagent.exceptions import TimeExceeded
from minisweagent.models.litellm_model import LitellmModel

from issue_agent.logging import get_logger
from issue_agent.runner.llm_errors import ProviderBudgetExceededError, budget_error

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

logger = get_logger(__name__)

# mini-swe-agent's tenacity wrapper reads its attempt cap from this variable on
# every ``query()`` (``minisweagent.models.utils.retry``).
_TENACITY_ATTEMPTS_ENV = "MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"
# Settings.llm_max_retries' default, for a model built without one.
_DEFAULT_MAX_RETRIES = 5


class CostUnavailableError(RuntimeError):
    """Raised (under ``cost_tracking="strict"``) when a step's cost can't be
    determined from either the provider response or LiteLLM's price map.

    Proceeding with ``0.0`` would silently disarm all three budget controls
    (per-task, daily, and the alert), so the task is aborted instead (R5.5.3).
    """


def _provider_reported_cost(response) -> float | None:
    """Cost the provider put in the response, or ``None`` if it didn't.

    Checks, in order: the ``additional_headers`` slot LiteLLM's OpenRouter handler
    fills from ``usage.cost``; a ``response_cost`` already resolved onto
    ``_hidden_params``; and ``usage.cost`` on the response body itself.
    """
    hidden = getattr(response, "_hidden_params", None) or {}

    try:
        header_cost = litellm.cost_calculator.get_response_cost_from_hidden_params(hidden)
    except Exception:  # pragma: no cover - defensive
        header_cost = None
    if header_cost is not None and header_cost > 0:
        return float(header_cost)

    if isinstance(hidden, dict):
        direct = hidden.get("response_cost")
        if direct is not None and direct > 0:
            return float(direct)

    usage = getattr(response, "usage", None)
    usage_cost = getattr(usage, "cost", None)
    if usage_cost is None and isinstance(usage, dict):
        usage_cost = usage.get("cost")
    if usage_cost is not None and usage_cost > 0:
        return float(usage_cost)

    return None


@contextmanager
def _tenacity_attempts(attempts: int) -> Iterator[None]:
    """Set mini-swe-agent's retry cap for the duration of one ``query()``."""
    previous = os.environ.get(_TENACITY_ATTEMPTS_ENV)
    os.environ[_TENACITY_ATTEMPTS_ENV] = str(attempts)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(_TENACITY_ATTEMPTS_ENV, None)
        else:
            os.environ[_TENACITY_ATTEMPTS_ENV] = previous


class CostAwareLitellmModel(LitellmModel):
    """``LitellmModel`` that prefers the provider-reported cost over the price map,
    and that keeps a hung provider from outliving the task's wall-time budget.

    Retries live in one layer: LiteLLM's own ``max_retries`` is forced to 0 and the
    configured ``max_retries`` becomes mini-swe-agent's tenacity attempt cap
    (``max_retries + 1``). Stacked, the two multiplied the per-request timeout —
    6 x 10 waits of 120s for one stalled call.

    Each attempt's timeout is capped at the time left of ``wall_time_limit_seconds``
    (counted from the first call), and an exhausted budget raises ``TimeExceeded``
    instead of another request.

    OpenRouter pins a conversation to one provider while its prompt cache is warm
    and re-routes only on an error, so a provider that accepts a request and never
    answers keeps getting every retry. From the second attempt on, a fresh
    ``session_id`` (the routing key) lets OpenRouter pick another provider.

    A provider spending-limit refusal (OpenRouter's budget guardrail arrives as
    a generic ``APIError``) is re-raised as ``ProviderBudgetExceededError``, an
    abort exception: every retry would be refused the same way.
    """

    abort_exceptions = [
        *LitellmModel.abort_exceptions,
        TimeExceeded,
        ProviderBudgetExceededError,
    ]

    def __init__(self, *, wall_time_limit_seconds: int = 0, **kwargs) -> None:
        super().__init__(**kwargs)
        self._wall_time_limit = wall_time_limit_seconds
        self._clock: Callable[[], float] = time.monotonic
        self._deadline: float | None = None
        self._attempt = 0

    def query(self, messages: list[dict], **kwargs) -> dict:
        self._attempt = 0
        max_retries = self.config.model_kwargs.get("max_retries", _DEFAULT_MAX_RETRIES)
        with _tenacity_attempts(max_retries + 1):
            return super().query(messages, **kwargs)

    def _query(self, messages: list[dict], **kwargs):
        self._attempt += 1
        call_kwargs = {**kwargs, "max_retries": 0}

        remaining = self._remaining_seconds()
        if remaining is not None:
            if remaining <= 0:
                raise TimeExceeded(
                    {
                        "role": "exit",
                        "content": "TimeExceeded",
                        "extra": {"exit_status": "TimeExceeded", "submission": ""},
                    }
                )
            request_timeout = call_kwargs.get("timeout", self.config.model_kwargs.get("timeout"))
            call_kwargs["timeout"] = (
                min(request_timeout, remaining) if request_timeout else remaining
            )

        if self._attempt > 1 and self._is_openrouter():
            extra_body = call_kwargs.get("extra_body", self.config.model_kwargs.get("extra_body"))
            call_kwargs["extra_body"] = {**(extra_body or {}), "session_id": uuid.uuid4().hex}

        try:
            return super()._query(messages, **call_kwargs)
        except Exception as exc:
            error = budget_error(exc)
            if error is not None:
                raise ProviderBudgetExceededError(error) from exc
            raise

    def _remaining_seconds(self) -> float | None:
        """Seconds left of the wall-time budget, or ``None`` when it is disabled."""
        if self._wall_time_limit <= 0:
            return None
        now = self._clock()
        if self._deadline is None:
            self._deadline = now + self._wall_time_limit
        return self._deadline - now

    def _is_openrouter(self) -> bool:
        provider = self.config.model_kwargs.get("custom_llm_provider")
        if provider:
            return provider == "openrouter"
        return self.config.model_name.partition("/")[0] == "openrouter"

    def _calculate_cost(self, response) -> dict[str, float]:
        provider_cost = _provider_reported_cost(response)
        if provider_cost is not None:
            return {"cost": provider_cost}

        try:
            cost = litellm.cost_calculator.completion_cost(response, model=self.config.model_name)
        except Exception as exc:
            return self._cost_unavailable(str(exc))

        if cost <= 0.0:
            return self._cost_unavailable("zero")

        return {"cost": float(cost)}

    def _cost_unavailable(self, reason: str) -> dict[str, float]:
        """Strict (``cost_tracking != "ignore_errors"``) aborts the task;
        lenient logs and carries on with ``0.0`` (the pre-R5.5 behavior)."""
        if self.config.cost_tracking == "ignore_errors":
            logger.warning("llm_cost_unavailable", model=self.config.model_name, error=reason)
            return {"cost": 0.0}
        logger.error(
            "llm_cost_unavailable",
            model=self.config.model_name,
            error=reason,
            action="task_aborted",
        )
        raise CostUnavailableError(
            f"cost for {self.config.model_name!r} could not be determined "
            f"({reason}); refusing to proceed with an unmetered budget"
        )

    # Allowlist, not a denylist of "known secrets" (same reasoning as
    # environment._ENV_ALLOWLIST): model_kwargs is whatever
    # agent_factory._model_kwargs() decides to pass to the LLM SDK, and a
    # future credential-bearing key added there (a cloud provider's secret
    # key, a custom auth header, ...) should have to be deliberately
    # allowlisted here before it can reach a rendered template.
    _TEMPLATE_SAFE_MODEL_KWARGS = ("max_retries",)

    def get_template_vars(self, **kwargs) -> dict:
        # Defense in depth for A1: model_kwargs carries LLM credentials
        # (api_key, and api_base when set — see agent_factory._model_kwargs).
        # Strip everything but the allowlisted fields from the namespace
        # Jinja renders system_template/instance_template with, so a
        # template that ever references {{ model_kwargs }} can't leak them.
        template_vars = super().get_template_vars(**kwargs)
        model_kwargs = {
            k: v
            for k, v in (template_vars.get("model_kwargs") or {}).items()
            if k in self._TEMPLATE_SAFE_MODEL_KWARGS
        }
        return {**template_vars, "model_kwargs": model_kwargs}
