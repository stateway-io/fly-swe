"""A hung LLM call must not outlive the task's wall-time budget.

mini-swe-agent checks ``wall_time_limit_seconds`` only *between* steps, and a
call that gets no bytes back is retried by two stacked layers (LiteLLM's
``max_retries`` x tenacity's 10 attempts), each waiting out the per-request
timeout — one stalled OpenRouter call cost a production task 48 minutes against
a 30-minute limit. ``CostAwareLitellmModel`` therefore owns the retry budget and
the deadline of every call it makes.
"""

from __future__ import annotations

import os

import litellm
import pytest
from minisweagent.exceptions import TimeExceeded
from minisweagent.models.litellm_model import LitellmModelConfig

from issue_agent.runner.litellm_cost import CostAwareLitellmModel
from issue_agent.runner.llm_errors import ProviderBudgetExceededError

_TENACITY_ENV = "MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"
_MESSAGES = [{"role": "user", "content": "hi"}]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Response:
    """What a successful ``litellm.completion`` returns, as far as ``_query`` cares."""


class _FakeCompletion:
    """Stand-in for ``litellm.completion`` that records every call's kwargs and
    advances a fake clock by ``seconds_per_call`` (a stalled request)."""

    def __init__(self, clock: _Clock, *, seconds_per_call: float = 0.0, fail: bool = True) -> None:
        self.clock = clock
        self.seconds_per_call = seconds_per_call
        self.fail = fail
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.clock.now += self.seconds_per_call
        if self.fail:
            raise litellm.Timeout("no bytes", model="m", llm_provider="openrouter")
        return _Response()


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """tenacity waits 4-60s between attempts; the tests must not."""
    monkeypatch.setattr("time.sleep", lambda _s: None)


def _model(
    clock: _Clock,
    *,
    model_name="openrouter/~deepseek/deepseek-v4-flash-latest",
    wall: int = 0,
    **model_kwargs,
) -> CostAwareLitellmModel:
    model = CostAwareLitellmModel(
        config_class=LitellmModelConfig,
        model_name=model_name,
        model_kwargs=model_kwargs,
        wall_time_limit_seconds=wall,
    )
    model._clock = clock
    return model


class TestWallDeadline:
    def test_caps_request_timeout_at_remaining_wall_time(self, monkeypatch):
        clock = _Clock()
        fake = _FakeCompletion(clock, fail=False)
        monkeypatch.setattr("litellm.completion", fake)
        model = _model(clock, wall=100, timeout=120)

        model._query(_MESSAGES)

        assert fake.calls[0]["timeout"] == pytest.approx(100)

    def test_keeps_request_timeout_when_budget_is_larger(self, monkeypatch):
        clock = _Clock()
        fake = _FakeCompletion(clock, fail=False)
        monkeypatch.setattr("litellm.completion", fake)
        model = _model(clock, wall=1800, timeout=120)

        model._query(_MESSAGES)

        assert fake.calls[0]["timeout"] == 120

    def test_deadline_counts_from_the_first_call_not_from_construction(self, monkeypatch):
        clock = _Clock()
        fake = _FakeCompletion(clock, fail=False)
        monkeypatch.setattr("litellm.completion", fake)
        model = _model(clock, wall=100, timeout=120)
        clock.now += 500  # workspace / toolchain setup before the agent starts

        model._query(_MESSAGES)

        assert fake.calls[0]["timeout"] == pytest.approx(100)

    def test_raises_time_exceeded_without_calling_the_provider(self, monkeypatch):
        clock = _Clock()
        fake = _FakeCompletion(clock, fail=False)
        monkeypatch.setattr("litellm.completion", fake)
        model = _model(clock, wall=100, timeout=120)
        model._query(_MESSAGES)  # starts the clock
        clock.now += 100

        with pytest.raises(TimeExceeded) as exc:
            model._query(_MESSAGES)

        assert len(fake.calls) == 1
        assert exc.value.messages[0]["extra"]["exit_status"] == "TimeExceeded"

    def test_no_cap_when_wall_limit_is_zero(self, monkeypatch):
        clock = _Clock()
        fake = _FakeCompletion(clock, fail=False)
        monkeypatch.setattr("litellm.completion", fake)
        model = _model(clock, wall=0, timeout=120)
        model._query(_MESSAGES)
        clock.now += 10_000

        model._query(_MESSAGES)

        assert fake.calls[1]["timeout"] == 120

    def test_stalled_retries_stop_at_the_deadline_and_are_not_retried_again(self, monkeypatch):
        clock = _Clock()
        fake = _FakeCompletion(clock, seconds_per_call=120)
        monkeypatch.setattr("litellm.completion", fake)
        model = _model(clock, wall=300, timeout=120, max_retries=9)

        with pytest.raises(TimeExceeded):
            model.query(_MESSAGES)

        # 120s + 120s + 60s (remaining) burn the 300s budget; the 4th attempt
        # is refused before any request goes out.
        assert [round(c["timeout"]) for c in fake.calls] == [120, 120, 60]


class TestRetryOwnership:
    def test_litellm_internal_retries_are_disabled(self, monkeypatch):
        clock = _Clock()
        fake = _FakeCompletion(clock, fail=False)
        monkeypatch.setattr("litellm.completion", fake)

        _model(clock, max_retries=5)._query(_MESSAGES)

        assert fake.calls[0]["max_retries"] == 0

    def test_attempts_follow_configured_max_retries(self, monkeypatch):
        clock = _Clock()
        fake = _FakeCompletion(clock)
        monkeypatch.setattr("litellm.completion", fake)

        with pytest.raises(litellm.Timeout):
            _model(clock, max_retries=2).query(_MESSAGES)

        assert len(fake.calls) == 3

    def test_tenacity_env_is_restored_after_the_call(self, monkeypatch):
        clock = _Clock()
        monkeypatch.setattr("litellm.completion", _FakeCompletion(clock))
        monkeypatch.setenv(_TENACITY_ENV, "7")

        with pytest.raises(litellm.Timeout):
            _model(clock, max_retries=1).query(_MESSAGES)

        assert os.environ[_TENACITY_ENV] == "7"

    def test_tenacity_env_is_removed_when_it_was_unset(self, monkeypatch):
        clock = _Clock()
        monkeypatch.setattr("litellm.completion", _FakeCompletion(clock))
        monkeypatch.delenv(_TENACITY_ENV, raising=False)

        with pytest.raises(litellm.Timeout):
            _model(clock, max_retries=1).query(_MESSAGES)

        assert _TENACITY_ENV not in os.environ

    def test_each_query_gets_a_fresh_attempt_budget(self, monkeypatch):
        clock = _Clock()
        fake = _FakeCompletion(clock)
        monkeypatch.setattr("litellm.completion", fake)
        model = _model(clock, max_retries=1)

        for _ in range(2):
            with pytest.raises(litellm.Timeout):
                model.query(_MESSAGES)

        assert len(fake.calls) == 4


class TestStickyRoutingReset:
    """OpenRouter pins a conversation to one provider while its prompt cache is
    warm and only re-routes on an *error* — a provider that accepts the request
    and never answers keeps receiving every retry. A new ``session_id`` per
    retry is a new routing key."""

    def test_first_attempt_leaves_routing_alone(self, monkeypatch):
        clock = _Clock()
        fake = _FakeCompletion(clock, fail=False)
        monkeypatch.setattr("litellm.completion", fake)

        _model(clock, max_retries=3)._query(_MESSAGES)

        assert "extra_body" not in fake.calls[0]

    def test_each_retry_uses_a_new_session_id(self, monkeypatch):
        clock = _Clock()
        fake = _FakeCompletion(clock)
        monkeypatch.setattr("litellm.completion", fake)

        with pytest.raises(litellm.Timeout):
            _model(clock, max_retries=2).query(_MESSAGES)

        ids = [c.get("extra_body", {}).get("session_id") for c in fake.calls]
        assert ids[0] is None
        assert ids[1] and ids[2] and ids[1] != ids[2]

    def test_a_new_query_starts_unpinned_again(self, monkeypatch):
        clock = _Clock()
        fake = _FakeCompletion(clock)
        monkeypatch.setattr("litellm.completion", fake)
        model = _model(clock, max_retries=1)

        for _ in range(2):
            with pytest.raises(litellm.Timeout):
                model.query(_MESSAGES)

        assert "extra_body" not in fake.calls[0]
        assert "extra_body" not in fake.calls[2]

    def test_existing_extra_body_is_preserved(self, monkeypatch):
        clock = _Clock()
        fake = _FakeCompletion(clock)
        monkeypatch.setattr("litellm.completion", fake)

        with pytest.raises(litellm.Timeout):
            _model(clock, max_retries=1, extra_body={"transforms": ["middle-out"]}).query(_MESSAGES)

        assert fake.calls[1]["extra_body"]["transforms"] == ["middle-out"]
        assert "session_id" in fake.calls[1]["extra_body"]

    def test_other_providers_never_get_a_session_id(self, monkeypatch):
        clock = _Clock()
        fake = _FakeCompletion(clock)
        monkeypatch.setattr("litellm.completion", fake)

        with pytest.raises(litellm.Timeout):
            _model(clock, model_name="anthropic/claude-sonnet-4-5", max_retries=2).query(_MESSAGES)

        assert all("extra_body" not in c for c in fake.calls)

    def test_explicit_openrouter_provider_counts(self, monkeypatch):
        clock = _Clock()
        fake = _FakeCompletion(clock)
        monkeypatch.setattr("litellm.completion", fake)

        with pytest.raises(litellm.Timeout):
            _model(
                clock,
                model_name="deepseek/deepseek-v4-flash",
                custom_llm_provider="openrouter",
                max_retries=1,
            ).query(_MESSAGES)

        assert "session_id" in fake.calls[1]["extra_body"]


class TestProviderBudgetAbort:
    """An exhausted provider spending limit (OpenRouter's
    budget guardrail, a 403 LiteLLM surfaces as a generic ``APIError``) is
    refused the same way on every retry — so it aborts on the first one."""

    _BODY = (
        '{"error":{"message":"Budget limit exceeded (monthly limit). $10.02 of '
        '$10.00 used; resets 2026-11-01T00:00:00.000Z.","code":403,"metadata":'
        '{"limit_source":"openrouter_guardrail_budget"}}}'
    )

    def _failing(self, exc: Exception):
        calls: list[dict] = []

        def completion(**kwargs):
            calls.append(kwargs)
            raise exc

        return completion, calls

    def test_budget_error_is_not_retried(self, monkeypatch):
        exc = litellm.APIError(
            status_code=500,
            message=f"OpenrouterException - {self._BODY}",
            llm_provider="openrouter",
            model="m",
        )
        completion, calls = self._failing(exc)
        monkeypatch.setattr("litellm.completion", completion)

        with pytest.raises(ProviderBudgetExceededError) as raised:
            _model(_Clock(), max_retries=5).query(_MESSAGES)

        assert len(calls) == 1
        assert raised.value.error.resets_at == "2026-11-01T00:00:00.000Z"
        assert raised.value.__cause__ is exc

    def test_other_api_errors_are_still_retried(self, monkeypatch):
        exc = litellm.APIError(
            status_code=500,
            message="OpenrouterException - [SSL: UNEXPECTED_EOF]",
            llm_provider="openrouter",
            model="m",
        )
        completion, calls = self._failing(exc)
        monkeypatch.setattr("litellm.completion", completion)

        with pytest.raises(litellm.APIError):
            _model(_Clock(), max_retries=2).query(_MESSAGES)

        assert len(calls) == 3
