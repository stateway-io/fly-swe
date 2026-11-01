"""Ingestion through a non-GitHub provider, end to end (PRD §5, Phase 2a).

The ``FakeProvider`` has its own headers, payload shape and no credentials
hint. If the dispatcher, the core filters or the queue still assumed
GitHub, these tests would fail; none of them makes a network call.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from issue_agent.providers.registry import ProviderRegistry
from issue_agent.queue.models import TaskStatus, TriggerKind
from issue_agent.webhook.routes import router as webhook_router
from issue_agent.worker import run_task
from tests.fakes.fake_provider import FakeProvider, fake_body, fake_headers

from .conftest import _make_settings

if TYPE_CHECKING:
    from issue_agent.config import Settings
    from issue_agent.queue.store import SQLiteStore

SECRET = "fake-forge-secret"
FAKE_INSTANCES = ("fake", "fake-no-association")


def _route_on_fake_instances(settings: Settings) -> Settings:
    """Route the configured repos on the fake instances. The FakeProvider
    has no config type, so `Settings` can't declare it under `providers`;
    the routes are rebound after validation."""
    settings.repo_routes = [
        route.model_copy(update={"provider": instance})
        for instance in FAKE_INSTANCES
        for route in settings.repo_routes
    ]
    return settings


@pytest.fixture
def worker_settings(worker_settings: Settings) -> Settings:
    return _route_on_fake_instances(worker_settings)


@pytest.fixture
def fake_app(store: SQLiteStore, monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> FastAPI:
    settings = _route_on_fake_instances(_make_settings(DATA_DIR=str(tmp_path / "webhook-data")))
    monkeypatch.setattr("issue_agent.webhook.routes.get_settings", lambda: settings)
    app = FastAPI()
    app.state.store = store
    app.state.registry = ProviderRegistry(
        {
            "fake": FakeProvider("fake", secret=SECRET),
            "fake-no-association": FakeProvider(
                "fake-no-association", secret=SECRET, reports_association=False
            ),
        }
    )
    app.include_router(webhook_router)
    return app


async def _post(
    app: FastAPI,
    instance: str,
    data: dict[str, Any],
    *,
    event: str = "issue",
    delivery: str = "fake-1",
    secret: str = SECRET,
):
    body = json.dumps(data).encode()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post(
            f"/webhooks/{instance}",
            content=body,
            headers=fake_headers(body, secret=secret, event=event, delivery=delivery),
        )


class TestFakeProviderIngestion:
    @pytest.mark.asyncio
    async def test_issue_opened_becomes_a_task_without_network(
        self, fake_app: FastAPI, store: SQLiteStore
    ) -> None:
        with patch("issue_agent.providers.github.auth.GithubIntegration") as mock_auth:
            resp = await _post(fake_app, "fake", fake_body())

        assert resp.status_code == 202
        mock_auth.assert_not_called()
        task = store.get_task(resp.json()["task_id"])
        assert task is not None
        assert task.provider_instance == "fake"
        assert task.credentials_ref is None
        assert task.label_category == "bug"
        trigger = task.trigger
        assert trigger is not None
        assert trigger.kind is TriggerKind.ISSUE_OPENED
        assert trigger.issue.provider_instance == "fake"
        assert store.is_delivery_processed("fake", "fake-1")

    @pytest.mark.asyncio
    async def test_bad_signature_is_rejected_and_not_recorded(
        self, fake_app: FastAPI, store: SQLiteStore
    ) -> None:
        resp = await _post(fake_app, "fake", fake_body(), secret="wrong")

        assert resp.status_code == 400
        assert resp.json() == {"error": "invalid signature"}
        assert not store.is_delivery_processed("fake", "fake-1")

    @pytest.mark.asyncio
    async def test_redelivery_is_a_duplicate(self, fake_app: FastAPI, store: SQLiteStore) -> None:
        await _post(fake_app, "fake", fake_body())
        again = await _post(fake_app, "fake", fake_body())

        assert again.json() == {"status": "duplicate", "delivery_id": "fake-1"}
        assert store.get_queue_depth() == 1

    @pytest.mark.asyncio
    async def test_labels_changed_without_trigger_label_is_dropped(
        self, fake_app: FastAPI, store: SQLiteStore
    ) -> None:
        resp = await _post(fake_app, "fake", fake_body("label_added", labels=["wontfix"]))

        assert resp.json() == {"status": "ignored", "reason": "no_trigger_label"}
        assert store.get_queue_depth() == 0

    @pytest.mark.asyncio
    async def test_unrouted_repo_is_dropped_first(
        self, fake_app: FastAPI, store: SQLiteStore
    ) -> None:
        resp = await _post(fake_app, "fake", fake_body(repo="someone/else"))

        assert resp.json() == {"status": "ignored", "reason": "repo_not_allowed"}

    @pytest.mark.asyncio
    async def test_unsupported_event_is_acked(self, fake_app: FastAPI, store: SQLiteStore) -> None:
        resp = await _post(fake_app, "fake", fake_body(), event="wiki")

        assert resp.json() == {"status": "ignored", "reason": "unsupported_event"}
        assert store.is_delivery_processed("fake", "fake-1")


class TestForgeWithoutAssociation:
    """§3.1: a forge that never reports associations. The webhook can't judge
    the commenter, so the event is enqueued and the worker decides before any
    LLM spend — silently, as the ingestion drop would have."""

    _STRANGER = {"id": 1, "author": {"login": "stranger"}, "body": "do it"}

    @pytest.mark.asyncio
    async def test_known_disallowed_association_is_dropped_at_the_webhook(
        self, fake_app: FastAPI, store: SQLiteStore
    ) -> None:
        stranger = {"id": 1, "author": {"login": "stranger", "association": "none"}}
        resp = await _post(
            fake_app, "fake", fake_body("comment_command", comment=stranger), event="comment"
        )

        assert resp.json() == {"status": "ignored", "reason": "comment_author_not_allowed"}

    @pytest.mark.asyncio
    async def test_unknown_association_is_enqueued_then_aborted_by_the_worker(
        self, fake_app: FastAPI, store: SQLiteStore, worker_settings: Settings
    ) -> None:
        resp = await _post(
            fake_app,
            "fake-no-association",
            fake_body("comment_command", comment=self._STRANGER),
            event="comment",
        )
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        task = store.fetch_next()
        assert task is not None
        with patch("issue_agent.worker._post_blocked_comment") as mock_post:
            run_task(store, task, worker_settings, fake_app.state.registry)

        final = store.get_task(task_id)
        assert final is not None
        assert final.status is TaskStatus.ABORTED_POLICY
        assert final.error == "comment_author_not_allowed"
        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_association_issue_author_passes_the_worker_guard(
        self, fake_app: FastAPI, store: SQLiteStore, worker_settings: Settings
    ) -> None:
        reporter = {"id": 2, "author": {"login": "reporter"}, "body": "more detail"}
        resp = await _post(
            fake_app,
            "fake-no-association",
            fake_body("comment_command", comment=reporter),
            event="comment",
        )
        task = store.fetch_next()
        assert task is not None

        # Stop right after the guards: reaching classification means the
        # comment-author guard let the issue author through.
        with patch(
            "issue_agent.worker.classify_labels", side_effect=RuntimeError("past guards")
        ) as classify:
            run_task(store, task, worker_settings, fake_app.state.registry)

        classify.assert_called_once()
        assert store.get_task(resp.json()["task_id"]).error == "unhandled_worker_exception"  # type: ignore[union-attr]


class TestWorkerOnTheFakeForge:
    """The worker's outbound side goes through the task's provider: a fake
    forge's issue is triaged end to end with no GitHub code involved."""

    @pytest.mark.asyncio
    async def test_triage_comment_lands_on_the_fake_forge(
        self, fake_app: FastAPI, store: SQLiteStore, worker_settings: Settings
    ) -> None:
        resp = await _post(fake_app, "fake", fake_body(labels=["question"]))
        assert resp.status_code == 202
        task = store.fetch_next()
        assert task is not None
        assert task.provider_instance == "fake"

        run_task(store, task, worker_settings, fake_app.state.registry)

        final = store.get_task(resp.json()["task_id"])
        assert final is not None
        assert final.status is TaskStatus.SUCCEEDED
        forge = fake_app.state.registry.get("fake").forge
        [comment] = forge.posted("stateway-io/stateway-issues", 7)
        assert "Triage" in comment
