from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.observability.metrics import get_daily_budget_metrics
from issue_agent.queue.store import SQLiteStore
from tests.helpers.settings import github_providers


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


def _make_settings(**overrides) -> Settings:
    defaults = dict(
        PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
        LLM_API_KEY="k",
        MAX_DAILY_BUDGET_USD=50.0,
    )
    defaults.update(overrides)
    return SettingsNoEnv(**defaults)


class TestGetDailyBudgetMetrics:
    def test_returns_zero_when_no_spend(self) -> None:
        store = MagicMock(spec=SQLiteStore)
        mock_budget = MagicMock()
        mock_budget.total_cost_usd = 0.0
        mock_budget.task_count = 0
        store.get_daily_budget.return_value = mock_budget
        settings = _make_settings()

        result = get_daily_budget_metrics(store, settings)

        assert result.date == datetime.now(UTC).strftime("%Y-%m-%d")
        assert result.total_cost_usd == 0.0
        assert result.task_count == 0
        assert result.max_budget_usd == 50.0
        assert result.remaining_usd == 50.0
        assert result.is_exceeded is False

    def test_returns_partial_spend(self) -> None:
        store = MagicMock(spec=SQLiteStore)
        mock_budget = MagicMock()
        mock_budget.total_cost_usd = 12.45
        mock_budget.task_count = 5
        store.get_daily_budget.return_value = mock_budget
        settings = _make_settings()

        result = get_daily_budget_metrics(store, settings)

        assert result.total_cost_usd == 12.45
        assert result.task_count == 5
        assert result.remaining_usd == 37.55
        assert result.is_exceeded is False

    def test_returns_exceeded_when_over_limit(self) -> None:
        store = MagicMock(spec=SQLiteStore)
        mock_budget = MagicMock()
        mock_budget.total_cost_usd = 75.0
        mock_budget.task_count = 15
        store.get_daily_budget.return_value = mock_budget
        settings = _make_settings(MAX_DAILY_BUDGET_USD=50.0)

        result = get_daily_budget_metrics(store, settings)

        assert result.total_cost_usd == 75.0
        assert result.remaining_usd == 0.0
        assert result.is_exceeded is True

    def test_exact_limit_not_exceeded(self) -> None:
        store = MagicMock(spec=SQLiteStore)
        mock_budget = MagicMock()
        mock_budget.total_cost_usd = 50.0
        mock_budget.task_count = 10
        store.get_daily_budget.return_value = mock_budget
        settings = _make_settings(MAX_DAILY_BUDGET_USD=50.0)

        result = get_daily_budget_metrics(store, settings)

        assert result.total_cost_usd == 50.0
        assert result.remaining_usd == 0.0
        assert result.is_exceeded is True

    def test_to_dict_returns_serializable_dict(self) -> None:
        store = MagicMock(spec=SQLiteStore)
        mock_budget = MagicMock()
        mock_budget.total_cost_usd = 10.0
        mock_budget.task_count = 3
        store.get_daily_budget.return_value = mock_budget
        settings = _make_settings()

        result = get_daily_budget_metrics(store, settings)
        d = result.to_dict()

        assert isinstance(d, dict)
        assert d["total_cost_usd"] == 10.0
        assert d["task_count"] == 3
        assert d["max_budget_usd"] == 50.0
        assert d["remaining_usd"] == 40.0
        assert d["is_exceeded"] is False
        assert "date" in d
