"""A Gitea instance behind the generic dispatcher (PRD-Gitea §1 success
criterion): real captured deliveries, re-signed, go through
``ProviderRegistry.from_settings`` and ``POST /webhooks/{instance_id}`` into
the queue — with no Gitea-specific code outside ``providers/gitea/``."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from issue_agent.providers.registry import ProviderRegistry
from issue_agent.webhook.routes import router as webhook_router
from tests.helpers.settings import github_providers

from .conftest import _make_settings

if TYPE_CHECKING:
    from issue_agent.queue.store import SQLiteStore

FIXTURES = Path(__file__).parents[1] / "unit" / "providers" / "gitea" / "fixtures"
INSTANCE = "gitea_corp"
SECRET = "gitea-webhook-secret"
REPO = "team/app"


@pytest.fixture
def app(store: SQLiteStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FastAPI:
    settings = _make_settings(
        DATA_DIR=str(tmp_path / "webhook-data"),
        PROVIDERS={
            **github_providers(),
            INSTANCE: {
                "type": "gitea",
                "host": "https://git.example.com",
                "bot_login": "issue-agent",
                "token": "tok",
                "webhook_secret": SECRET,
            },
        },
        REPO_ROUTES=[{"issues": REPO, "provider": INSTANCE, "target": REPO}],
    )
    monkeypatch.setattr("issue_agent.webhook.routes.get_settings", lambda: settings)
    app = FastAPI()
    app.state.store = store
    app.state.registry = ProviderRegistry.from_settings(settings)
    app.include_router(webhook_router)
    return app


def _delivery(
    fixture: str, *, delivery: str = "uuid-1", secret: str = SECRET
) -> tuple[bytes, dict[str, str]]:
    body = (FIXTURES / f"{fixture}.json").read_bytes()
    captured = json.loads((FIXTURES / "headers" / f"{fixture}.json").read_text())
    return body, {
        "X-Gitea-Event": captured["X-Gitea-Event"],
        "X-Gitea-Event-Type": captured["X-Gitea-Event-Type"],
        "X-Gitea-Delivery": delivery,
        "X-Gitea-Signature": hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
    }


async def _post(app: FastAPI, fixture: str, *, path: str = INSTANCE, **kw: Any) -> Any:
    body, headers = _delivery(fixture, **kw)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post(f"/webhooks/{path}", content=body, headers=headers)


class TestGiteaStack:
    @pytest.mark.asyncio
    async def test_trigger_label_enqueues_a_gitea_task(self, app: FastAPI, store: SQLiteStore):
        response = await _post(app, "issues_label_updated_with_trigger")

        assert response.status_code == 202, response.json()
        task = store.get_task(response.json()["task_id"])
        assert task.provider_instance == INSTANCE
        assert (task.repo, task.target_repo) == (REPO, REPO)
        assert task.delivery_id.startswith("gitea-")

    @pytest.mark.asyncio
    async def test_replay_with_a_new_delivery_uuid_is_a_duplicate(
        self, app: FastAPI, store: SQLiteStore
    ):
        first = await _post(app, "issues_label_updated_with_trigger", delivery="uuid-1")
        replay = await _post(app, "issues_label_updated_with_trigger", delivery="uuid-2")

        assert first.status_code == 202
        assert replay.status_code == 200
        assert replay.json()["status"] == "duplicate"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("fixture", "reason"),
        [
            # label_cleared still lists the old labels; the adapter empties them.
            ("issues_label_cleared", "no_trigger_label"),
            ("issues_label_updated_without_trigger", "no_trigger_label"),
            ("issue_comment_on_pull_request", "comment_on_pull_request"),
            ("issue_comment_own_bot", "own_bot_comment"),
            ("issue_comment_edited", "unhandled_event_action"),
            ("push", "unsupported_event"),
        ],
    )
    async def test_non_triggers_are_acked_without_a_task(
        self, app: FastAPI, store: SQLiteStore, fixture: str, reason: str
    ):
        response = await _post(app, fixture)

        assert response.status_code == 200
        assert response.json() == {"status": "ignored", "reason": reason}

    @pytest.mark.asyncio
    async def test_wrong_secret_is_rejected_and_does_not_burn_the_delivery(
        self, app: FastAPI, store: SQLiteStore
    ):
        forged = await _post(app, "issues_label_updated_with_trigger", secret="nope")
        real = await _post(app, "issues_label_updated_with_trigger")

        assert forged.status_code == 400
        assert real.status_code == 202

    @pytest.mark.asyncio
    async def test_the_path_picks_the_provider(self, app: FastAPI):
        response = await _post(app, "issues_label_updated_with_trigger", path="github")

        # Gitea's compat X-GitHub-* headers aren't sent here, and even if
        # they were, the GitHub instance has its own secret.
        assert response.status_code == 400
