"""Two GitHub App instances behind one service (PRD §3.5/§3.6).

github.com and a GHE host, each with its own webhook secret, route and
idempotency space. The same delivery id and the same `owner/repo#N` on both
must stay two independent events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from issue_agent.providers.registry import ProviderRegistry
from issue_agent.webhook.routes import router as webhook_router
from tests.helpers.settings import github_providers

from .conftest import _make_settings, make_github_payload, sign_payload

if TYPE_CHECKING:
    from issue_agent.queue.store import SQLiteStore

REPO = "stateway-io/stateway-issues"
GHE_SECRET = "ghe-webhook-secret"


@pytest.fixture
def two_instance_app(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> FastAPI:
    settings = _make_settings(
        DATA_DIR=str(tmp_path / "webhook-data"),
        PROVIDERS={
            **github_providers(),
            **github_providers("ghe", webhook_secret=GHE_SECRET, host="https://ghe.example.com"),
        },
        REPO_ROUTES=[
            {"issues": REPO, "provider": "github", "target": "stateway-io/public"},
            {"issues": REPO, "provider": "ghe", "target": "stateway-io/internal"},
        ],
    )
    monkeypatch.setattr("issue_agent.webhook.routes.get_settings", lambda: settings)
    app = FastAPI()
    app.state.store = store
    app.state.registry = ProviderRegistry.from_settings(settings)
    app.include_router(webhook_router)
    return app


async def _post(app: FastAPI, instance: str, secret: str | None = None) -> object:
    body = make_github_payload(repo=REPO, issue_number=7, labels=["bug"])
    signature = sign_payload(body, secret) if secret else sign_payload(body)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post(
            f"/webhooks/{instance}",
            content=body,
            headers={
                "X-GitHub-Delivery": "same-delivery-id",
                "X-GitHub-Event": "issues",
                "X-Hub-Signature-256": signature,
            },
        )


class TestTwoGitHubInstances:
    @pytest.mark.asyncio
    async def test_same_delivery_on_both_instances_makes_two_tasks(
        self, two_instance_app: FastAPI, store: SQLiteStore
    ) -> None:
        github = await _post(two_instance_app, "github")
        ghe = await _post(two_instance_app, "ghe", GHE_SECRET)

        assert github.status_code == 202
        assert ghe.status_code == 202
        github_task = store.get_task(github.json()["task_id"])
        ghe_task = store.get_task(ghe.json()["task_id"])
        assert (github_task.provider_instance, github_task.target_repo) == (
            "github",
            "stateway-io/public",
        )
        assert (ghe_task.provider_instance, ghe_task.target_repo) == (
            "ghe",
            "stateway-io/internal",
        )
        assert store.is_delivery_processed("github", "same-delivery-id")
        assert store.is_delivery_processed("ghe", "same-delivery-id")

    @pytest.mark.asyncio
    async def test_redelivery_is_a_duplicate_only_on_its_own_instance(
        self, two_instance_app: FastAPI, store: SQLiteStore
    ) -> None:
        await _post(two_instance_app, "ghe", GHE_SECRET)

        again = await _post(two_instance_app, "ghe", GHE_SECRET)

        assert again.json()["status"] == "duplicate"
        assert store.get_queue_depth() == 1

    @pytest.mark.asyncio
    async def test_each_instance_checks_its_own_secret(
        self, two_instance_app: FastAPI, store: SQLiteStore
    ) -> None:
        # Signed with github.com's secret, delivered to the GHE instance.
        resp = await _post(two_instance_app, "ghe")

        assert resp.status_code == 400
        assert store.get_queue_depth() == 0
        assert not store.is_delivery_processed("ghe", "same-delivery-id")
