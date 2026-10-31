from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.core.models import TriggerKind
from issue_agent.queue.models import Task, TaskStatus
from issue_agent.queue.store import SQLiteStore
from issue_agent.webhook.backfill import backfill_trigger_json
from tests.helpers.factories import make_task
from tests.helpers.settings import github_providers

REPO = "stateway-io/stateway-issues"


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file=None, env_prefix="", extra="ignore", yaml_file=None,
    )


@pytest.fixture
def settings() -> Settings:
    return SettingsNoEnv(
        PROVIDERS=github_providers(app_id=1, app_private_key="k", webhook_secret="s"),
        LLM_API_KEY="k",
    )


@pytest.fixture
def store() -> SQLiteStore:
    s = SQLiteStore(":memory:")
    s.init()
    return s


def _issues_payload(action: str = "opened", **issue: Any) -> dict[str, Any]:
    return {
        "action": action,
        "issue": {
            "number": 42,
            "title": "Broken link",
            "body": "The README has a broken link.",
            "state": "open",
            "labels": [{"name": "docs"}],
            "user": {"login": "reporter"},
            **issue,
        },
        "repository": {"full_name": REPO},
        "sender": {"login": "labeler"},
        "installation": {"id": 987},
    }


def _comment_payload(**comment: Any) -> dict[str, Any]:
    payload = _issues_payload("created")
    payload["sender"] = {"login": "maintainer"}
    payload["comment"] = {
        "id": 900,
        "user": {"login": "maintainer", "type": "User"},
        "author_association": "MEMBER",
        "body": "please fix",
        "created_at": "2026-09-08T10:00:00Z",
        **comment,
    }
    return payload


def _legacy_task(
    store: SQLiteStore,
    payload: Any,
    *,
    kind: TriggerKind = TriggerKind.ISSUE_OPENED,
    status: TaskStatus = TaskStatus.PENDING,
    delivery_id: str = "d1",
    issue_number: int = 42,
) -> str:
    """Insert a row as a pre-v6 deployment would have: raw payload only."""
    task_id = store.insert_task(
        Task(
            delivery_id=delivery_id,
            repo=REPO,
            issue_number=issue_number,
            trigger_kind=kind,
            payload_json=payload if isinstance(payload, str) else json.dumps(payload),
        )
    )
    if status is not TaskStatus.PENDING:
        store.update_status(task_id, status)
    return task_id


def _get(store: SQLiteStore, task_id: str) -> Task:
    task = store.get_task(task_id)
    assert task is not None
    return task


class TestBackfill:
    def test_renormalizes_a_legacy_issue_task(
        self, store: SQLiteStore, settings: Settings
    ) -> None:
        task_id = _legacy_task(store, _issues_payload("labeled"), kind=TriggerKind.LABEL_ADDED)

        assert backfill_trigger_json(store, settings) == 1

        task = _get(store, task_id)
        trigger = task.trigger
        assert trigger is not None
        assert trigger.kind is TriggerKind.LABEL_ADDED
        assert trigger.delivery_key == "d1"
        assert trigger.issue.repo == REPO
        assert trigger.issue.title == "Broken link"
        assert trigger.issue.labels == ["docs"]
        assert trigger.actor.login == "labeler"
        assert task.provider_instance == "github"
        # From the payload's installation, like at ingestion.
        assert task.credentials_ref == "987"
        assert task.status is TaskStatus.PENDING

    def test_comment_task_keeps_its_trigger_comment(
        self, store: SQLiteStore, settings: Settings
    ) -> None:
        task_id = _legacy_task(
            store, _comment_payload(), kind=TriggerKind.COMMENT_COMMAND
        )

        backfill_trigger_json(store, settings)

        trigger = _get(store, task_id).trigger
        assert trigger is not None
        assert trigger.trigger_comment is not None
        assert trigger.trigger_comment.id == "900"
        assert trigger.trigger_comment.author.association == "member"

    def test_ingestion_filters_are_not_reapplied(
        self, store: SQLiteStore, settings: Settings
    ) -> None:
        # Accepted under an older config; the current one would drop it.
        settings.comments_author_filter_allow_associations = []
        task_id = _legacy_task(
            store, _comment_payload(), kind=TriggerKind.COMMENT_COMMAND
        )

        backfill_trigger_json(store, settings)

        assert _get(store, task_id).trigger is not None

    def test_payload_without_installation_has_no_credentials_ref(
        self, store: SQLiteStore, settings: Settings
    ) -> None:
        payload = _issues_payload()
        del payload["installation"]
        task_id = _legacy_task(store, payload)

        backfill_trigger_json(store, settings)

        task = _get(store, task_id)
        assert task.trigger is not None
        assert task.credentials_ref is None

    @pytest.mark.parametrize(
        "payload",
        ["{}", "not json", "null", json.dumps(_issues_payload(number="x"))],
    )
    def test_unrecoverable_live_task_fails_terminally(
        self, store: SQLiteStore, settings: Settings, payload: str
    ) -> None:
        task_id = _legacy_task(store, payload)

        assert backfill_trigger_json(store, settings) == 0

        task = _get(store, task_id)
        assert task.trigger_json is None
        assert task.status is TaskStatus.FAILED_TERMINAL
        assert task.error == "invalid_trigger"

    def test_kind_mismatch_is_unrecoverable(
        self, store: SQLiteStore, settings: Settings
    ) -> None:
        # The row says comment_command but the payload is an `opened` event.
        task_id = _legacy_task(
            store, _issues_payload(), kind=TriggerKind.COMMENT_COMMAND
        )

        backfill_trigger_json(store, settings)

        assert _get(store, task_id).status is TaskStatus.FAILED_TERMINAL

    @pytest.mark.parametrize(
        "status", [TaskStatus.SUCCEEDED, TaskStatus.ABORTED_POLICY]
    )
    def test_unrecoverable_finished_task_is_left_alone(
        self, store: SQLiteStore, settings: Settings, status: TaskStatus
    ) -> None:
        task_id = _legacy_task(store, "{}", status=status)

        backfill_trigger_json(store, settings)

        task = _get(store, task_id)
        assert task.trigger_json is None
        assert task.status is status
        assert task.error is None

    def test_live_statuses_all_fail_when_unrecoverable(
        self, store: SQLiteStore, settings: Settings
    ) -> None:
        ids = [
            _legacy_task(store, "{}", status=status, delivery_id=status.value, issue_number=n)
            for n, status in enumerate(
                [TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.FAILED_RECOVERABLE],
                start=1,
            )
        ]

        backfill_trigger_json(store, settings)

        assert {_get(store, i).status for i in ids} == {TaskStatus.FAILED_TERMINAL}

    def test_idempotent(self, store: SQLiteStore, settings: Settings) -> None:
        _legacy_task(store, _issues_payload())

        assert backfill_trigger_json(store, settings) == 1
        assert backfill_trigger_json(store, settings) == 0
        assert store.find_tasks_without_trigger() == []

    def test_tasks_created_with_a_trigger_are_untouched(
        self, store: SQLiteStore, settings: Settings
    ) -> None:
        task = make_task()
        task_id = store.insert_task(task)

        assert backfill_trigger_json(store, settings) == 0
        assert _get(store, task_id).trigger_json == task.trigger_json


class TestBackfillWithoutGitHubInstance:
    """Legacy rows are GitHub payloads of the `github` instance; with that
    instance renamed or gone they can't be re-normalized."""

    def test_rows_are_left_for_the_worker_guard(self, store: SQLiteStore) -> None:
        settings = SettingsNoEnv(
            PROVIDERS=github_providers("ghe", app_id=1), LLM_API_KEY="k"
        )
        task_id = _legacy_task(store, _issues_payload())

        assert backfill_trigger_json(store, settings) == 0

        task = _get(store, task_id)
        assert task.trigger is None
        assert task.status is TaskStatus.PENDING
