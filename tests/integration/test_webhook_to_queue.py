from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from issue_agent.queue.models import TaskStatus, TriggerKind

from .conftest import make_github_payload, sign_payload


class TestMissingDeliveryHeader:
    @pytest.mark.asyncio
    async def test_returns_400(self, app: AsyncClient) -> None:
        body = make_github_payload()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 400
        assert "missing X-GitHub-Delivery" in resp.json()["error"]


class TestProviderDispatch:
    """``POST /webhooks/{instance_id}`` picks the provider by path (PRD §3.4)."""

    @pytest.mark.asyncio
    async def test_unknown_instance_is_404_and_touches_nothing(
        self, app: AsyncClient, store
    ) -> None:
        body = make_github_payload()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/gitlab",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-unknown-instance",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 404
        assert resp.json() == {"error": "unknown provider instance"}
        assert not store.is_delivery_processed("github", "delivery-unknown-instance")
        assert store.get_queue_depth() == 0

    @pytest.mark.asyncio
    async def test_github_app_url_routes_to_the_github_instance(
        self, app: AsyncClient, store
    ) -> None:
        body = make_github_payload(labels=["bug"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-github-instance",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 202
        task = store.get_task(resp.json()["task_id"])
        assert task is not None
        assert task.provider_instance == "github"


class TestUnsupportedEvent:
    """Events outside the allowlist (ping, pull_request, push, ...) are acked.

    GitHub treats a non-2xx as a failed delivery, so a merged PR must not
    show up as a wall of 400s just because the App also subscribes to
    ``pull_request``/``push``. The signature is still checked first.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("event", ["ping", "pull_request", "push", "check_suite"])
    async def test_signed_unsupported_event_is_acked_as_ignored(
        self, app: AsyncClient, store, event: str
    ) -> None:
        body = make_github_payload()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": f"delivery-unsupported-{event}",
                    "X-GitHub-Event": event,
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored", "reason": "unsupported_event"}
        assert store.is_delivery_processed("github", f"delivery-unsupported-{event}")
        assert store.get_queue_depth() == 0

    @pytest.mark.asyncio
    async def test_redelivered_unsupported_event_is_a_duplicate(
        self, app: AsyncClient, store
    ) -> None:
        # The duplicate check now runs before the adapter parses the event
        # (PRD Phase 2a), so a redelivery is acked as a duplicate — still a 2xx,
        # still nothing created.
        body = make_github_payload()
        headers = {
            "X-GitHub-Delivery": "delivery-unsupported-twice",
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": sign_payload(body),
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            first = await c.post("/webhooks/github", content=body, headers=headers)
            second = await c.post("/webhooks/github", content=body, headers=headers)
        assert first.json() == {"status": "ignored", "reason": "unsupported_event"}
        assert second.status_code == 200
        assert second.json()["status"] == "duplicate"
        assert store.get_queue_depth() == 0

    @pytest.mark.asyncio
    async def test_unsigned_unsupported_event_is_still_rejected(
        self, app: AsyncClient, store
    ) -> None:
        body = make_github_payload()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-unsupported-badsig",
                    "X-GitHub-Event": "pull_request",
                    "X-Hub-Signature-256": sign_payload(body, secret="wrong-secret"),
                },
            )
        assert resp.status_code == 400
        assert "invalid signature" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_unsupported_event_is_logged(self, app: AsyncClient) -> None:
        body = make_github_payload()
        with patch("issue_agent.webhook.routes.logger") as mock_logger:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post(
                    "/webhooks/github",
                    content=body,
                    headers={
                        "X-GitHub-Delivery": "delivery-unsupported-log",
                        "X-GitHub-Event": "pull_request",
                        "X-Hub-Signature-256": sign_payload(body),
                    },
                )
        bound = mock_logger.bind.return_value
        bound.info.assert_any_call("unsupported_event")
        mock_logger.bind.assert_any_call(
            delivery_id="delivery-unsupported-log", event="pull_request"
        )


class TestInvalidSignature:
    @pytest.mark.asyncio
    async def test_wrong_secret_returns_400(self, app: AsyncClient) -> None:
        body = make_github_payload()
        wrong_sig = sign_payload(body, secret="wrong-secret")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-002",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": wrong_sig,
                },
            )
        assert resp.status_code == 400
        assert "invalid signature" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_tampered_body_returns_400(self, app: AsyncClient) -> None:
        body = make_github_payload()
        sig = sign_payload(body)
        tampered = body.replace(b"opened", b"closed")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=tampered,
                headers={
                    "X-GitHub-Delivery": "delivery-003",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sig,
                },
            )
        assert resp.status_code == 400
        assert "invalid signature" in resp.json()["error"]


class TestDuplicateDelivery:
    @pytest.mark.asyncio
    async def test_second_request_returns_200_duplicate(self, app: AsyncClient, store) -> None:
        body = make_github_payload()
        sig = sign_payload(body)
        headers = {
            "X-GitHub-Delivery": "delivery-010",
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": sig,
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            first = await c.post("/webhooks/github", content=body, headers=headers)
            second = await c.post("/webhooks/github", content=body, headers=headers)

        assert first.status_code == 202
        data = second.json()
        assert second.status_code == 200
        assert data["status"] == "duplicate"
        assert data["delivery_id"] == "delivery-010"


class TestInvalidJson:
    @pytest.mark.asyncio
    async def test_non_json_body_returns_400(self, app: AsyncClient) -> None:
        body = b"not json at all"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-004",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 400
        assert "invalid JSON" in resp.json()["error"]


class TestInvalidPayload:
    @pytest.mark.asyncio
    async def test_payload_that_fails_normalization_returns_400(
        self, app: AsyncClient, store
    ) -> None:
        body = b'{"action": "opened", "issue": {"number": "not-a-number"}}'
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-invalid-payload",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 400
        assert resp.json() == {"error": "invalid payload"}
        assert store.get_queue_depth() == 0
        assert store.is_delivery_processed("github", "delivery-invalid-payload")


class TestRejectionsAreLogged:
    """Every 400 leaves an application log line saying why.

    Uvicorn's access line is all ``flyctl logs`` shows otherwise, and the
    four 400 branches are indistinguishable from it.
    """

    @staticmethod
    async def _post(app, body: bytes, headers: dict[str, str]):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return await c.post("/webhooks/github", content=body, headers=headers)

    @pytest.mark.asyncio
    async def test_missing_delivery_header_is_logged(self, app: AsyncClient) -> None:
        body = make_github_payload()
        with patch("issue_agent.webhook.routes.logger") as mock_logger:
            resp = await self._post(
                app,
                body,
                {"X-GitHub-Event": "issues", "X-Hub-Signature-256": sign_payload(body)},
            )
        assert resp.status_code == 400
        mock_logger.warning.assert_any_call("missing_delivery_header")

    @pytest.mark.asyncio
    async def test_invalid_signature_is_logged_with_event(self, app: AsyncClient) -> None:
        body = make_github_payload()
        with patch("issue_agent.webhook.routes.logger") as mock_logger:
            resp = await self._post(
                app,
                body,
                {
                    "X-GitHub-Delivery": "delivery-log-sig",
                    "X-GitHub-Event": "pull_request",
                    "X-Hub-Signature-256": sign_payload(body, secret="wrong-secret"),
                },
            )
        assert resp.status_code == 400
        mock_logger.bind.assert_any_call(delivery_id="delivery-log-sig", event="pull_request")
        mock_logger.bind.return_value.warning.assert_any_call("invalid_signature")

    @pytest.mark.asyncio
    async def test_invalid_json_is_logged(self, app: AsyncClient) -> None:
        body = b"not json at all"
        with patch("issue_agent.webhook.routes.logger") as mock_logger:
            resp = await self._post(
                app,
                body,
                {
                    "X-GitHub-Delivery": "delivery-log-json",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 400
        mock_logger.bind.return_value.warning.assert_any_call("invalid_json")


class TestRepoNotAllowed:
    @pytest.mark.asyncio
    async def test_unknown_repo_returns_200_ignored(self, app: AsyncClient) -> None:
        body = make_github_payload(repo="some-other/repo")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-005",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ignored"
        assert data["reason"] == "repo_not_allowed"

    @pytest.mark.asyncio
    async def test_unknown_repo_wins_over_comment_filters(self, app: AsyncClient) -> None:
        # The repo route is checked before the parser's ingestion filters,
        # as it was when they lived inline in the handler.
        body = make_github_payload(
            event="issue_comment",
            action="created",
            repo="some-other/repo",
            is_pull_request=True,
            comment={"user": {"type": "User", "login": "octocat"}},
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-005b",
                    "X-GitHub-Event": "issue_comment",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 200
        assert resp.json()["reason"] == "repo_not_allowed"


class TestUnhandledEventAction:
    @pytest.mark.asyncio
    async def test_deleted_issue_returns_200_ignored(self, app: AsyncClient) -> None:
        body = make_github_payload(action="deleted")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-006",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ignored"
        assert data["reason"] == "unhandled_event_action"


class TestIssueLifecycle:
    """R7.1, R7.2: issues.closed / issues.reopened are handled in
    the webhook process — closing or touching the ``issue_work`` row — with
    no Task enqueued, no GitHub API call and no LLM."""

    async def _post(self, app, body, delivery):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": delivery,
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )

    @pytest.mark.asyncio
    async def test_closed_with_open_work_closes_it_without_a_task(self, app, store) -> None:
        store.open_issue_work(
            "github",
            "stateway-io/stateway-issues",
            7,
            "stateway-io/stateway-agent-fixtures",
            "main",
            "agent/issue-7-x",
        )
        body = make_github_payload(action="closed", issue_number=7)

        resp = await self._post(app, body, "d-closed-1")

        assert resp.status_code == 200
        assert resp.json() == {"status": "handled", "reason": "issue_work_closed"}
        assert store.get_queue_depth() == 0
        work = store.get_issue_work("github", "stateway-io/stateway-issues", 7)
        assert work.state == "closed"
        assert work.closed_reason == "issue_closed"

    @pytest.mark.asyncio
    async def test_closed_with_no_work_is_a_noop(self, app, store) -> None:
        body = make_github_payload(action="closed", issue_number=8)

        resp = await self._post(app, body, "d-closed-2")

        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored", "reason": "no_open_work"}
        assert store.get_queue_depth() == 0
        assert store.get_issue_work("github", "stateway-io/stateway-issues", 8) is None

    @pytest.mark.asyncio
    async def test_closed_with_already_closed_work_is_a_noop(self, app, store) -> None:
        store.open_issue_work(
            "github",
            "stateway-io/stateway-issues",
            9,
            "stateway-io/stateway-agent-fixtures",
            "main",
            "agent/issue-9-x",
        )
        store.close_issue_work("github", "stateway-io/stateway-issues", 9, "pr_opened", pr_number=5)
        body = make_github_payload(action="closed", issue_number=9)

        resp = await self._post(app, body, "d-closed-3")

        assert resp.json()["reason"] == "no_open_work"
        work = store.get_issue_work("github", "stateway-io/stateway-issues", 9)
        assert work.closed_reason == "pr_opened"  # unchanged

    @pytest.mark.asyncio
    async def test_reopened_does_not_reopen_work_and_enqueues_nothing(self, app, store) -> None:
        store.open_issue_work(
            "github",
            "stateway-io/stateway-issues",
            10,
            "stateway-io/stateway-agent-fixtures",
            "main",
            "agent/issue-10-x",
        )
        store.close_issue_work("github", "stateway-io/stateway-issues", 10, "max_attempts")
        body = make_github_payload(action="reopened", issue_number=10)

        resp = await self._post(app, body, "d-reopened-1")

        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored", "reason": "issue_reopened"}
        assert store.get_queue_depth() == 0
        work = store.get_issue_work("github", "stateway-io/stateway-issues", 10)
        assert work.state == "closed"
        assert work.closed_reason == "max_attempts"


class TestValidWebhook:
    @pytest.mark.asyncio
    async def test_returns_202_with_task_id(self, app: AsyncClient, store) -> None:
        body = make_github_payload(
            issue_number=42,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-007",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["delivery_id"] == "delivery-007"
        assert "task_id" in data
        assert data["task_id"]

    @pytest.mark.asyncio
    async def test_task_persisted_in_store(self, app: AsyncClient, store) -> None:
        body = make_github_payload(
            issue_number=99,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-008",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        task = store.get_task(task_id)
        assert task is not None
        assert task.issue_number == 99
        assert task.repo == "stateway-io/stateway-issues"
        assert task.trigger_kind == TriggerKind.ISSUE_OPENED
        assert task.status == TaskStatus.PENDING
        # Populated from the resolved RepoRoute + webhook payload
        # (R5.2/R5.3, Task 2 of the multi-repo migration).
        assert task.target_repo == "stateway-io/stateway-agent-fixtures"
        assert task.base_branch == "main"
        assert task.workflow == "trunk_based"
        # The canonical trigger is what the worker reads (PRD §3.1).
        trigger = task.trigger
        assert trigger is not None
        assert trigger.kind is TriggerKind.ISSUE_OPENED
        assert trigger.delivery_key == "delivery-008"
        assert trigger.issue.repo == "stateway-io/stateway-issues"
        assert trigger.issue.number == 99
        assert task.provider_instance == "github"
        assert task.credentials_ref == "12345"

    @pytest.mark.asyncio
    async def test_delivery_marked_processed(self, app: AsyncClient, store) -> None:
        body = make_github_payload(
            issue_number=77,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-009",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 202
        assert store.is_delivery_processed("github", "delivery-009") is True

    @pytest.mark.asyncio
    async def test_bug_label_sets_label_category(self, app: AsyncClient, store) -> None:
        body = make_github_payload(issue_number=102, labels=["bug"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-011",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 202
        task = store.get_task(resp.json()["task_id"])
        assert task.label_category == "bug"

    @pytest.mark.asyncio
    async def test_docs_label_sets_label_category(self, app: AsyncClient, store) -> None:
        body = make_github_payload(issue_number=103, labels=["docs"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-012",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 202
        task = store.get_task(resp.json()["task_id"])
        assert task.label_category == "docs"

    @pytest.mark.asyncio
    async def test_no_labels_leaves_label_category_none(self, app: AsyncClient, store) -> None:
        body = make_github_payload(issue_number=104)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-013",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 202
        task = store.get_task(resp.json()["task_id"])
        assert task.label_category is None

    @pytest.mark.asyncio
    async def test_labeled_event_creates_task(self, app: AsyncClient, store) -> None:
        body = make_github_payload(
            action="labeled",
            issue_number=101,
            labels=["bug"],
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": "delivery-010",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]
        task = store.get_task(task_id)
        assert task is not None
        assert task.trigger_kind == TriggerKind.LABEL_ADDED

    async def _post_labeled(self, app, delivery_id: str, labels: list[str]):
        body = make_github_payload(action="labeled", issue_number=102, labels=labels)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": delivery_id,
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("labels", [[], ["wontfix"], ["priority:low", "good first issue"]])
    async def test_labeled_without_trigger_label_is_ignored(
        self, app: AsyncClient, store, labels: list[str]
    ) -> None:
        # Before 1c this became a task that aborted with "out of scope" on
        # every unrelated label (PRD §3.1: label_added = the set changed).
        resp = await self._post_labeled(app, "delivery-nolabel", labels)

        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored", "reason": "no_trigger_label"}
        assert store.fetch_next() is None
        assert store.is_delivery_processed("github", "delivery-nolabel")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("labels", [["wontfix", "bug"], ["agent-skip"]])
    async def test_labeled_with_trigger_label_in_the_set_creates_task(
        self, app: AsyncClient, store, labels: list[str]
    ) -> None:
        # The set is judged, not the label just added: an unrelated label on
        # an issue that already carries `bug` still counts, and `agent-skip`
        # still gets its acknowledgement.
        resp = await self._post_labeled(app, "delivery-withlabel", labels)

        assert resp.status_code == 202
        assert store.get_task(resp.json()["task_id"]) is not None

    @pytest.mark.asyncio
    async def test_labeled_event_ignored_when_issue_already_active(
        self, app: AsyncClient, store
    ) -> None:
        # Simulate the GitHub double-delivery when an issue is opened with a
        # label: opened creates the first task, labeled is dropped as redundant.
        opened_body = make_github_payload(action="opened", issue_number=110, labels=["bug"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            opened_resp = await c.post(
                "/webhooks/github",
                content=opened_body,
                headers={
                    "X-GitHub-Delivery": "delivery-opened-110",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(opened_body),
                },
            )
        assert opened_resp.status_code == 202

        labeled_body = make_github_payload(action="labeled", issue_number=110, labels=["bug"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            labeled_resp = await c.post(
                "/webhooks/github",
                content=labeled_body,
                headers={
                    "X-GitHub-Delivery": "delivery-labeled-110",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(labeled_body),
                },
            )
        assert labeled_resp.status_code == 200
        assert labeled_resp.json()["reason"] == "redundant_label_event"
        # Only the opened task should exist for this issue.
        assert store.get_queue_depth() == 1

    @pytest.mark.asyncio
    async def test_labeled_event_creates_task_when_no_active_task(
        self, app: AsyncClient, store
    ) -> None:
        # A label added later (when the issue has no pending/running task)
        # should still create a task.
        labeled_body = make_github_payload(action="labeled", issue_number=111, labels=["bug"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/webhooks/github",
                content=labeled_body,
                headers={
                    "X-GitHub-Delivery": "delivery-labeled-111",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(labeled_body),
                },
            )
        assert resp.status_code == 202
        task = store.get_task(resp.json()["task_id"])
        assert task is not None
        assert task.trigger_kind == TriggerKind.LABEL_ADDED

    @pytest.mark.asyncio
    async def test_opened_event_ignored_when_labeled_already_active(
        self, app: AsyncClient, store
    ) -> None:
        """The reverse of test_labeled_event_ignored_when_issue_already_active:
        if `issues.labeled` happens to be processed before `issues.opened`
        for the same issue (GitHub gives no delivery-order guarantee between
        the two), `opened` must be the one dropped as redundant instead of
        creating a second task."""
        labeled_body = make_github_payload(action="labeled", issue_number=112, labels=["bug"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            labeled_resp = await c.post(
                "/webhooks/github",
                content=labeled_body,
                headers={
                    "X-GitHub-Delivery": "delivery-labeled-112",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(labeled_body),
                },
            )
        assert labeled_resp.status_code == 202

        opened_body = make_github_payload(action="opened", issue_number=112, labels=["bug"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            opened_resp = await c.post(
                "/webhooks/github",
                content=opened_body,
                headers={
                    "X-GitHub-Delivery": "delivery-opened-112",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(opened_body),
                },
            )
        assert opened_resp.status_code == 200
        assert opened_resp.json()["reason"] == "redundant_issue_opened_event"
        assert store.get_queue_depth() == 1

    @pytest.mark.asyncio
    async def test_opened_and_labeled_concurrent_only_creates_one_task(
        self, app: AsyncClient, store
    ) -> None:
        """GitHub delivers `issues.opened` and `issues.labeled` almost
        simultaneously for an issue created with labels, with no guaranteed
        order. Posting them concurrently must still yield exactly one task
        for the issue, whichever request the server happens to process
        first — this is what insert_task_unless_active_creation_task()'s
        atomic check-then-insert closes (see queue/store.py)."""
        opened_body = make_github_payload(action="opened", issue_number=113, labels=["bug"])
        labeled_body = make_github_payload(action="labeled", issue_number=113, labels=["bug"])

        async def _post(body: bytes, delivery: str, event: str = "issues"):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                return await c.post(
                    "/webhooks/github",
                    content=body,
                    headers={
                        "X-GitHub-Delivery": delivery,
                        "X-GitHub-Event": event,
                        "X-Hub-Signature-256": sign_payload(body),
                    },
                )

        opened_resp, labeled_resp = await asyncio.gather(
            _post(opened_body, "delivery-opened-113"),
            _post(labeled_body, "delivery-labeled-113"),
        )

        statuses = {opened_resp.status_code, labeled_resp.status_code}
        assert statuses == {200, 202}
        assert store.get_queue_depth() == 1

        winner = opened_resp if opened_resp.status_code == 202 else labeled_resp
        loser = labeled_resp if winner is opened_resp else opened_resp
        assert loser.json()["reason"] in (
            "redundant_label_event",
            "redundant_issue_opened_event",
        )
        task = store.get_task(winner.json()["task_id"])
        assert task is not None
        assert task.issue_number == 113

    @pytest.mark.asyncio
    async def test_labeled_event_not_blocked_by_unrelated_comment_task(
        self, app: AsyncClient, store
    ) -> None:
        """A comment-triggered task already active on the issue must not
        cause a genuinely new `issues.labeled` event to be dropped — only
        another issue_opened/label_added task counts as "redundant" (Fix
        2's scoping of has_active_creation_task_for_issue)."""
        comment_body = make_github_payload(
            event="issue_comment",
            action="created",
            issue_number=114,
            issue_author="octocat",
            comment={
                "user": {"type": "User", "login": "octocat"},
                "author_association": "NONE",
            },
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            comment_resp = await c.post(
                "/webhooks/github",
                content=comment_body,
                headers={
                    "X-GitHub-Delivery": "delivery-comment-114",
                    "X-GitHub-Event": "issue_comment",
                    "X-Hub-Signature-256": sign_payload(comment_body),
                },
            )
        assert comment_resp.status_code == 202
        comment_task = store.get_task(comment_resp.json()["task_id"])
        assert comment_task.trigger_kind == TriggerKind.COMMENT_COMMAND

        labeled_body = make_github_payload(action="labeled", issue_number=114, labels=["bug"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            labeled_resp = await c.post(
                "/webhooks/github",
                content=labeled_body,
                headers={
                    "X-GitHub-Delivery": "delivery-labeled-114",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sign_payload(labeled_body),
                },
            )
        assert labeled_resp.status_code == 202
        assert store.get_queue_depth() == 2


class TestCommentFiltering:
    async def _post(self, app, body, delivery):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Delivery": delivery,
                    "X-GitHub-Event": "issue_comment",
                    "X-Hub-Signature-256": sign_payload(body),
                },
            )

    @pytest.mark.asyncio
    async def test_comment_on_pull_request_is_ignored(self, app, store) -> None:
        body = make_github_payload(
            event="issue_comment",
            action="created",
            issue_number=1,
            is_pull_request=True,
            comment={
                "user": {"type": "User", "login": "octocat"},
                "author_association": "OWNER",
            },
        )
        resp = await self._post(app, body, "d-pr-comment")
        assert resp.status_code == 200
        assert resp.json()["reason"] == "comment_on_pull_request"
        assert store.get_queue_depth() == 0

    @pytest.mark.asyncio
    async def test_own_bot_comment_is_ignored(self, app, store) -> None:
        body = make_github_payload(
            event="issue_comment",
            action="created",
            issue_number=1,
            comment={"performed_via_github_app": {"id": 123}, "user": {"type": "Bot"}},
        )
        resp = await self._post(app, body, "d-own-bot")
        assert resp.status_code == 200
        assert resp.json()["reason"] == "own_bot_comment"
        assert store.get_queue_depth() == 0

    @pytest.mark.asyncio
    async def test_own_bot_comment_without_performed_via_app_is_ignored(
        self, app_with_bot_login, store
    ) -> None:
        # Regression: when performed_via_github_app is absent and agent.name
        # does not match the real bot login, app_bot_login must still identify
        # the bot and drop its own comments.
        body = make_github_payload(
            event="issue_comment",
            action="created",
            issue_number=1,
            comment={
                "user": {"type": "Bot", "login": "stateway-issue-agent[bot]"},
                "author_association": "NONE",
            },
        )
        resp = await self._post(app_with_bot_login, body, "d-own-bot-no-app")
        assert resp.status_code == 200
        assert resp.json()["reason"] == "own_bot_comment"
        assert store.get_queue_depth() == 0

    @pytest.mark.asyncio
    async def test_comment_on_closed_issue_is_ignored(self, app, store) -> None:
        body = make_github_payload(
            event="issue_comment",
            action="created",
            issue_number=3,
            issue_state="closed",
            comment={
                "user": {"type": "User", "login": "octocat"},
                "author_association": "OWNER",
            },
        )
        resp = await self._post(app, body, "d-closed")
        assert resp.status_code == 200
        assert resp.json()["reason"] == "issue_closed"
        assert store.get_queue_depth() == 0

    @pytest.mark.asyncio
    async def test_issue_author_comment_creates_task(self, app, store) -> None:
        body = make_github_payload(
            event="issue_comment",
            action="created",
            issue_number=4,
            issue_author="octocat",
            comment={
                "user": {"type": "User", "login": "octocat"},
                "author_association": "NONE",
            },
        )
        resp = await self._post(app, body, "d-issue-author")
        assert resp.status_code == 202
        task = store.get_task(resp.json()["task_id"])
        assert task.trigger_kind == TriggerKind.COMMENT_COMMAND

    @pytest.mark.asyncio
    async def test_member_comment_creates_task(self, app, store) -> None:
        body = make_github_payload(
            event="issue_comment",
            action="created",
            issue_number=5,
            issue_author="reporter",
            comment={
                "user": {"type": "User", "login": "maintainer"},
                "author_association": "MEMBER",
            },
        )
        resp = await self._post(app, body, "d-member")
        assert resp.status_code == 202
        task = store.get_task(resp.json()["task_id"])
        assert task.trigger_kind == TriggerKind.COMMENT_COMMAND

    @pytest.mark.asyncio
    async def test_third_party_comment_is_dropped_silently(self, app, store) -> None:
        body = make_github_payload(
            event="issue_comment",
            action="created",
            issue_number=6,
            issue_author="reporter",
            comment={
                "user": {"type": "User", "login": "drive-by"},
                "author_association": "NONE",
            },
        )
        resp = await self._post(app, body, "d-third-party")
        assert resp.status_code == 200
        assert resp.json()["reason"] == "comment_author_not_allowed"
        assert store.get_queue_depth() == 0

    @pytest.mark.asyncio
    async def test_other_bot_comment_is_dropped_silently(self, app, store) -> None:
        body = make_github_payload(
            event="issue_comment",
            action="created",
            issue_number=7,
            issue_author="reporter",
            comment={
                "user": {"type": "Bot", "login": "dependabot[bot]"},
                "author_association": "NONE",
            },
        )
        resp = await self._post(app, body, "d-other-bot")
        assert resp.status_code == 200
        assert resp.json()["reason"] == "comment_author_not_allowed"
        assert store.get_queue_depth() == 0

    @pytest.mark.asyncio
    async def test_allowlisted_bot_comment_creates_task(self, app, store, monkeypatch) -> None:
        from .conftest import _make_settings

        settings = _make_settings(COMMENTS_AUTHOR_FILTER_ALLOWLIST=["dependabot[bot]"])
        monkeypatch.setattr("issue_agent.webhook.routes.get_settings", lambda: settings)
        body = make_github_payload(
            event="issue_comment",
            action="created",
            issue_number=8,
            issue_author="reporter",
            comment={
                "user": {"type": "Bot", "login": "dependabot[bot]"},
                "author_association": "NONE",
            },
        )
        resp = await self._post(app, body, "d-allowlisted-bot")
        assert resp.status_code == 202
        task = store.get_task(resp.json()["task_id"])
        assert task.trigger_kind == TriggerKind.COMMENT_COMMAND

    @pytest.mark.asyncio
    async def test_bot_that_opened_the_issue_keeps_its_channel(self, app, store) -> None:
        # The reporter is a bot; a maintainer just reopened the issue. Its
        # follow-up comment must still create a task (user directive, R2.1).
        body = make_github_payload(
            event="issue_comment",
            action="created",
            issue_number=9,
            issue_state="open",
            issue_author="watchdog[bot]",
            comment={
                "user": {"type": "Bot", "login": "watchdog[bot]"},
                "author_association": "NONE",
            },
        )
        resp = await self._post(app, body, "d-bot-author")
        assert resp.status_code == 202
        task = store.get_task(resp.json()["task_id"])
        assert task.trigger_kind == TriggerKind.COMMENT_COMMAND


class TestCredentialsHint:
    """R5.2 / PRD §7 risk 2: the webhook never makes a network call to resolve
    the installation; a task without one is resolved later by the worker."""

    @pytest.mark.asyncio
    async def test_payload_installation_becomes_the_credentials_ref(
        self, app: AsyncClient, store
    ) -> None:
        body = make_github_payload(issue_number=200, installation_id=555)
        with patch("issue_agent.providers.github.auth.GithubIntegration") as mock_auth:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/webhooks/github",
                    content=body,
                    headers={
                        "X-GitHub-Delivery": "delivery-installation-1",
                        "X-GitHub-Event": "issues",
                        "X-Hub-Signature-256": sign_payload(body),
                    },
                )
        assert resp.status_code == 202
        mock_auth.assert_not_called()

        task = store.get_task(resp.json()["task_id"])
        assert task is not None
        assert task.credentials_ref == "555"

    @pytest.mark.asyncio
    async def test_absent_installation_is_left_for_the_worker(
        self, app: AsyncClient, store
    ) -> None:
        body = make_github_payload(issue_number=201, installation_id=None)
        with patch("issue_agent.providers.github.auth.GithubIntegration") as mock_auth:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/webhooks/github",
                    content=body,
                    headers={
                        "X-GitHub-Delivery": "delivery-installation-2",
                        "X-GitHub-Event": "issues",
                        "X-Hub-Signature-256": sign_payload(body),
                    },
                )
        assert resp.status_code == 202
        mock_auth.assert_not_called()

        task = store.get_task(resp.json()["task_id"])
        assert task is not None
        # No hint: the provider plugin resolves credentials per repo.
        assert task.credentials_ref is None
        assert task.trigger is not None
        assert task.trigger.credentials_ref is None


class TestInvalidSignatureDoesNotBurnTheDelivery:
    """A request that fails authentication must leave no trace in the
    idempotency table: otherwise anyone who learns a delivery id (they are
    visible in GitHub's delivery log) can post a forged request with it and
    have the genuine delivery acked as a duplicate and dropped."""

    @pytest.mark.asyncio
    async def test_forged_request_does_not_make_the_real_delivery_a_duplicate(
        self, app: AsyncClient, store
    ) -> None:
        body = make_github_payload(issue_number=301)
        delivery = {"X-GitHub-Delivery": "delivery-forged", "X-GitHub-Event": "issues"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            forged = await c.post(
                "/webhooks/github",
                content=body,
                headers={
                    **delivery,
                    "X-Hub-Signature-256": sign_payload(body, secret="wrong-secret"),
                },
            )
            assert forged.status_code == 400
            assert store.is_delivery_processed("github", "delivery-forged") is False

            genuine = await c.post(
                "/webhooks/github",
                content=body,
                headers={**delivery, "X-Hub-Signature-256": sign_payload(body)},
            )

        assert genuine.status_code == 202
        assert genuine.json().get("status") != "duplicate"
        assert store.get_queue_depth() == 1


class TestDeliveryNotMarkedProcessedOnMidFlightFailure:
    """The delivery is marked processed only after
    the task row exists. If anything between signature verification and the
    insert raises, FastAPI answers 500 and GitHub retries the same
    X-GitHub-Delivery — which must then go through, not be swallowed as a
    duplicate. (The failure used to be the installation lookup; since PRD
    Phase 2a the handler makes no network call, so the insert is what can fail.)"""

    @pytest.mark.asyncio
    async def test_insert_failure_leaves_delivery_unprocessed(
        self, app: AsyncClient, store
    ) -> None:
        body = make_github_payload(issue_number=202)
        with patch.object(
            store,
            "insert_task_unless_active_creation_task",
            side_effect=RuntimeError("database is locked"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                with pytest.raises(RuntimeError, match="database is locked"):
                    await c.post(
                        "/webhooks/github",
                        content=body,
                        headers={
                            "X-GitHub-Delivery": "delivery-insert-fail",
                            "X-GitHub-Event": "issues",
                            "X-Hub-Signature-256": sign_payload(body),
                        },
                    )

        assert store.is_delivery_processed("github", "delivery-insert-fail") is False
        assert store.get_queue_depth() == 0

    @pytest.mark.asyncio
    async def test_retry_after_transient_failure_succeeds_and_creates_task(
        self, app: AsyncClient, store
    ) -> None:
        body = make_github_payload(issue_number=203)
        headers = {
            "X-GitHub-Delivery": "delivery-insert-retry",
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": sign_payload(body),
        }

        with patch.object(
            store,
            "insert_task_unless_active_creation_task",
            side_effect=RuntimeError("transient"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                with pytest.raises(RuntimeError):
                    await c.post("/webhooks/github", content=body, headers=headers)

        assert store.is_delivery_processed("github", "delivery-insert-retry") is False

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            retry_resp = await c.post("/webhooks/github", content=body, headers=headers)

        assert retry_resp.status_code == 202
        assert retry_resp.json()["status"] == "accepted"
        assert store.is_delivery_processed("github", "delivery-insert-retry") is True
        assert store.get_task(retry_resp.json()["task_id"]) is not None
