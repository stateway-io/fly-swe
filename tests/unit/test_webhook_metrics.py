from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from issue_agent.observability.prometheus_metrics import observe_task_result
from issue_agent.webhook.metrics import router as metrics_router


@pytest.fixture
def app() -> FastAPI:
    _app = FastAPI()
    _app.state.store = MagicMock()
    _app.include_router(metrics_router)
    return _app


def _client(app: FastAPI):
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestMetricsEndpoint:
    @pytest.mark.asyncio
    async def test_returns_200(self, app: FastAPI) -> None:
        async with _client(app) as c:
            resp = await c.get("/metrics")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_content_type_prometheus(self, app: FastAPI) -> None:
        async with _client(app) as c:
            resp = await c.get("/metrics")
        ct = resp.headers["content-type"]
        assert "text/plain" in ct or "openmetrics-text" in ct

    @pytest.mark.asyncio
    async def test_contains_metric_headers(self, app: FastAPI) -> None:
        async with _client(app) as c:
            resp = await c.get("/metrics")
        body = resp.text

        assert "# HELP issue_agent_tasks_total" in body
        assert "# TYPE issue_agent_tasks_total counter" in body
        assert "# HELP issue_agent_task_duration_seconds" in body
        assert "# TYPE issue_agent_task_duration_seconds histogram" in body
        assert "# HELP issue_agent_llm_cost_usd_total" in body
        assert "# TYPE issue_agent_llm_cost_usd_total counter" in body
        assert "# HELP issue_agent_iterations" in body
        assert "# TYPE issue_agent_iterations histogram" in body
        assert "# HELP issue_agent_queue_depth" in body
        assert "# TYPE issue_agent_queue_depth gauge" in body
        assert "# HELP issue_agent_budget_used_today_usd" in body
        assert "# TYPE issue_agent_budget_used_today_usd gauge" in body

    @pytest.mark.asyncio
    async def test_reflects_recorded_samples(self, app: FastAPI) -> None:
        observe_task_result("succeeded", 15.0, 2.5, 5, "owner/repo", provider_instance="github")
        observe_task_result("failed", 30.0, 1.0, 3, "owner/repo", provider_instance="github")

        async with _client(app) as c:
            resp = await c.get("/metrics")

        body = resp.text
        assert 'status="succeeded"' in body
        assert 'status="failed"' in body
        assert 'status="aborted"' in body
        assert 'repo="owner/repo"' in body
        assert 'provider_instance="github"' in body
        assert "issue_agent_task_duration_seconds_sum" in body
        assert "issue_agent_llm_cost_usd_total" in body
        assert "issue_agent_iterations_sum" in body
