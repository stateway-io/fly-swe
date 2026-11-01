from __future__ import annotations

import pytest
from minisweagent.models.litellm_model import LitellmModelConfig

from issue_agent.runner.litellm_cost import (
    CostAwareLitellmModel,
    CostUnavailableError,
    _provider_reported_cost,
)


class _Usage:
    def __init__(self, cost=None):
        if cost is not None:
            self.cost = cost


class _Response:
    def __init__(self, hidden_params=None, usage=None):
        self._hidden_params = hidden_params if hidden_params is not None else {}
        if usage is not None:
            self.usage = usage


def _model(**kwargs) -> CostAwareLitellmModel:
    return CostAwareLitellmModel(
        config_class=LitellmModelConfig,
        model_name="openrouter/deepseek/deepseek-v4-flash-0731",
        **kwargs,
    )


def _openrouter_response(cost: float) -> _Response:
    return _Response(
        hidden_params={"additional_headers": {"llm_provider-x-litellm-response-cost": cost}}
    )


class TestProviderReportedCost:
    def test_reads_openrouter_additional_header(self):
        assert _provider_reported_cost(_openrouter_response(0.0123)) == pytest.approx(0.0123)

    def test_reads_response_cost_key(self):
        assert _provider_reported_cost(
            _Response(hidden_params={"response_cost": 0.02})
        ) == pytest.approx(0.02)

    def test_reads_usage_cost_attr(self):
        assert _provider_reported_cost(_Response(usage=_Usage(0.03))) == pytest.approx(0.03)

    def test_reads_usage_cost_dict(self):
        assert _provider_reported_cost(_Response(usage={"cost": 0.04})) == pytest.approx(0.04)

    def test_none_when_nothing_reported(self):
        assert _provider_reported_cost(_Response(usage=_Usage())) is None

    def test_ignores_zero_and_negative(self):
        assert _provider_reported_cost(_openrouter_response(0.0)) is None


class TestCalculateCost:
    def test_prefers_provider_cost_over_price_map(self, monkeypatch):
        called = False

        def _boom(*a, **k):
            nonlocal called
            called = True
            raise AssertionError("price map should not be consulted")

        monkeypatch.setattr("litellm.cost_calculator.completion_cost", _boom)
        assert _model()._calculate_cost(_openrouter_response(0.5)) == {"cost": 0.5}
        assert called is False

    def test_falls_back_to_price_map(self, monkeypatch):
        monkeypatch.setattr("litellm.cost_calculator.completion_cost", lambda *a, **k: 0.07)
        assert _model()._calculate_cost(_Response()) == {"cost": 0.07}

    def test_strict_raises_when_price_map_errors(self, monkeypatch):
        monkeypatch.setattr(
            "litellm.cost_calculator.completion_cost",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("model not in map")),
        )
        with pytest.raises(CostUnavailableError):
            _model(cost_tracking="default")._calculate_cost(_Response())

    def test_strict_raises_when_price_map_returns_zero(self, monkeypatch):
        monkeypatch.setattr("litellm.cost_calculator.completion_cost", lambda *a, **k: 0.0)
        with pytest.raises(CostUnavailableError):
            _model(cost_tracking="default")._calculate_cost(_Response())

    def test_lenient_returns_zero_on_price_map_error(self, monkeypatch):
        monkeypatch.setattr(
            "litellm.cost_calculator.completion_cost",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("model not in map")),
        )
        assert _model(cost_tracking="ignore_errors")._calculate_cost(_Response()) == {"cost": 0.0}

    def test_lenient_returns_zero_when_price_map_returns_zero(self, monkeypatch):
        monkeypatch.setattr("litellm.cost_calculator.completion_cost", lambda *a, **k: 0.0)
        assert _model(cost_tracking="ignore_errors")._calculate_cost(_Response()) == {"cost": 0.0}

    def test_strict_does_not_raise_when_provider_reports_cost(self, monkeypatch):
        monkeypatch.setattr(
            "litellm.cost_calculator.completion_cost",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be consulted")),
        )
        assert _model(cost_tracking="default")._calculate_cost(_openrouter_response(0.9)) == {
            "cost": 0.9
        }


class TestGetTemplateVars:
    def test_strips_api_key_and_api_base(self):
        model = _model(
            model_kwargs={"api_key": "sk-secret", "api_base": "https://x", "max_retries": 3}
        )
        vars_ = model.get_template_vars()
        assert "api_key" not in vars_["model_kwargs"]
        assert "api_base" not in vars_["model_kwargs"]
        assert "sk-secret" not in str(vars_)
        assert vars_["model_kwargs"]["max_retries"] == 3

    def test_keeps_other_fields(self):
        model = _model()
        vars_ = model.get_template_vars()
        assert vars_["model_name"] == "openrouter/deepseek/deepseek-v4-flash-0731"
