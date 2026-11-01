"""Prometheus metrics are served only by the internal exporter.

The public FastAPI app (the Fly `http_service`, port 8080) must not serve
`/metrics`; the exporter started by `start_metrics_server` on
`metrics_port` (9090, not exposed by Fly) is the only way to scrape them.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from prometheus_client import generate_latest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.main import app
from issue_agent.observability.prometheus_metrics import init_metrics, observe_task_result
from tests.helpers.settings import github_providers


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


@pytest.fixture(autouse=True)
def _histograms() -> None:
    # The bucket-configurable histograms are created lazily at startup.
    init_metrics(
        SettingsNoEnv(
            PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
            LLM_API_KEY="k",
        )
    )


class TestPublicAppDoesNotServeMetrics:
    @pytest.mark.asyncio
    async def test_metrics_route_is_not_mounted(self) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/metrics")
        assert resp.status_code == 404

    def test_no_metrics_path_in_routes(self) -> None:
        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/metrics" not in paths


class TestInternalExporterPayload:
    def test_contains_metric_headers(self) -> None:
        body = generate_latest().decode("utf-8")

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

    def test_reflects_recorded_samples(self) -> None:
        observe_task_result("succeeded", 15.0, 2.5, 5, "owner/repo", provider_instance="github")
        observe_task_result("failed", 30.0, 1.0, 3, "owner/repo", provider_instance="github")

        body = generate_latest().decode("utf-8")
        assert 'status="succeeded"' in body
        assert 'status="failed"' in body
        assert 'repo="owner/repo"' in body
        assert 'provider_instance="github"' in body
        assert "issue_agent_task_duration_seconds_sum" in body
        assert "issue_agent_llm_cost_usd_total" in body
        assert "issue_agent_iterations_sum" in body
