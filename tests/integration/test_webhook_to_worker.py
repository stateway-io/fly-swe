from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from issue_agent.providers.github.client import GitHubForgeClient
from issue_agent.queue.models import TaskStatus
from issue_agent.runner.prompts import load_system_prompt
from issue_agent.worker import run_task

from .conftest import make_github_payload, sign_payload

if TYPE_CHECKING:
    from issue_agent.config import Settings
    from issue_agent.queue.store import SQLiteStore


def _task_events(store: SQLiteStore, task_id: str) -> list[dict]:
    return [
        {"type": e.event_type, "payload": json.loads(e.payload_json)}
        for e in store.get_task_events(task_id)
    ]


def _audit_lines(settings: Settings) -> list[str]:
    today = time.strftime("%Y-%m-%d", time.gmtime())
    audit_path = Path(settings.data_dir) / f"audit.{today}.log"
    if not audit_path.exists():
        return []
    return audit_path.read_text().strip().split("\n")


class TestNoLabels:
    @pytest.mark.asyncio
    async def test_flow(
        self, store: SQLiteStore, app: AsyncClient, worker_settings: Settings
    ) -> None:
        body = make_github_payload(issue_number=10)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-2-7-1",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )

        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        task = store.fetch_next()
        assert task is not None
        assert task.id == task_id
        assert task.status is TaskStatus.RUNNING
        # Populated at webhook-ingestion time from the resolved RepoRoute /
        # payload installation.
        assert task.target_repo == "stateway-io/stateway-agent-fixtures"
        assert task.base_branch == "main"
        assert task.workflow == "trunk_based"

        run_task(store, task, worker_settings, app.state.registry)

        final = store.get_task(task_id)
        assert final is not None
        assert final.status is TaskStatus.ABORTED_POLICY
        assert "skipped_by_policy" in (final.error or "")

        events = _task_events(store, task_id)
        assert any(e["type"] == "state_change" for e in events)

        audit = _audit_lines(worker_settings)
        assert any(
            "task_processed" in line and "aborted_policy" in line and '"action": "skip"' in line
            for line in audit
        )


class TestQuestionLabel:
    @pytest.mark.asyncio
    async def test_flow(
        self, store: SQLiteStore, app: AsyncClient, worker_settings: Settings
    ) -> None:
        body = make_github_payload(issue_number=20, labels=["question"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-2-7-2",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )

        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        task = store.fetch_next()
        assert task is not None
        assert task.status is TaskStatus.RUNNING

        with patch.object(GitHubForgeClient, "post_comment") as mock_post:
            run_task(store, task, worker_settings, app.state.registry)

        final = store.get_task(task_id)
        assert final is not None
        assert final.status is TaskStatus.SUCCEEDED

        mock_post.assert_called_once()
        assert mock_post.call_args.args[0] == 20

        events = _task_events(store, task_id)
        assert any(
            e["type"] == "state_change" and e["payload"].get("to") == "succeeded" for e in events
        )

        audit = _audit_lines(worker_settings)
        assert any(
            "task_processed" in line
            and '"outcome": "succeeded"' in line
            and '"action": "triage_only"' in line
            for line in audit
        )


class TestBugLabel:
    @pytest.mark.asyncio
    async def test_flow(
        self, store: SQLiteStore, app: AsyncClient, worker_settings: Settings
    ) -> None:
        body = make_github_payload(issue_number=30, labels=["bug"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-2-7-3",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )

        assert resp.status_code == 202
        assert "task_id" in resp.json()

        task = store.fetch_next()
        assert task is not None
        assert task.status is TaskStatus.RUNNING
        assert task.label_category == "bug"

        with patch("issue_agent.runner.executor.run_task") as mock_run:
            run_task(store, task, worker_settings, app.state.registry)

        mock_run.assert_called_once_with(
            store, task, worker_settings, app.state.registry.get("github")
        )

    @pytest.mark.asyncio
    async def test_label_category_reaches_bug_fix_prompt(
        self, store: SQLiteStore, app: AsyncClient, worker_settings: Settings
    ) -> None:
        """End-to-end proof: a `bug`-labeled webhook payload results in
        `Task.label_category == "bug"`, and that value actually resolves
        bug_fix.md's content via `load_system_prompt` — closing the
        structural dead path where `label_category` was always `None` and
        the category-specific prompt files were unreachable in production.
        """
        body = make_github_payload(issue_number=31, labels=["bug"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-2-7-3b",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )

        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        task = store.get_task(task_id)
        assert task is not None
        assert task.label_category == "bug"

        system_prompt = load_system_prompt(task.label_category, worker_settings.prompt_category_map)
        assert "classified as a **bug**" in system_prompt
        assert "Reproduce the bug" in system_prompt


class TestAgentSkipLabel:
    @pytest.mark.asyncio
    async def test_flow(
        self, store: SQLiteStore, app: AsyncClient, worker_settings: Settings
    ) -> None:
        body = make_github_payload(issue_number=40, labels=["agent-skip"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-2-7-4",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )

        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        task = store.fetch_next()
        assert task is not None
        assert task.status is TaskStatus.RUNNING

        run_task(store, task, worker_settings, app.state.registry)

        final = store.get_task(task_id)
        assert final is not None
        assert final.status is TaskStatus.ABORTED_POLICY
        assert "skipped_by_policy" in (final.error or "")

        events = _task_events(store, task_id)
        assert any(e["type"] == "state_change" for e in events)

        audit = _audit_lines(worker_settings)
        assert any(
            "task_processed" in line and "aborted_policy" in line and '"action": "skip"' in line
            for line in audit
        )
