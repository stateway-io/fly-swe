from __future__ import annotations

import json
import sqlite3

import pytest

from issue_agent.queue.models import Task, TaskStatus, TriggerKind
from issue_agent.queue.store import SQLiteStore


@pytest.fixture
def store() -> SQLiteStore:
    s = SQLiteStore(":memory:")
    s.init()
    return s


def _make_task(**overrides: dict) -> Task:
    defaults = dict(
        delivery_id="delivery-1",
        repo="stateway-io/stateway-issues",
        issue_number=42,
        trigger_kind=TriggerKind.ISSUE_OPENED,
        payload_json=json.dumps({"action": "opened", "issue": {"number": 42}}),
    )
    defaults.update(overrides)
    return Task(**defaults)


class TestDeliveryIdempotency:
    def test_fresh_delivery_not_processed(self, store: SQLiteStore) -> None:
        assert store.is_delivery_processed("github", "delivery-abc") is False

    def test_mark_delivery_and_check(self, store: SQLiteStore) -> None:
        store.mark_delivery_processed("github", "delivery-abc", "issues", "opened")
        assert store.is_delivery_processed("github", "delivery-abc") is True

    def test_mark_delivery_idempotent(self, store: SQLiteStore) -> None:
        store.mark_delivery_processed("github", "delivery-abc", "issues", "opened")
        store.mark_delivery_processed("github", "delivery-abc", "issues", "opened")
        assert store.is_delivery_processed("github", "delivery-abc") is True

    def test_multiple_deliveries_independent(self, store: SQLiteStore) -> None:
        store.mark_delivery_processed("github", "d1", "issues", "opened")
        store.mark_delivery_processed("github", "d2", "issues", "labeled")
        assert store.is_delivery_processed("github", "d1") is True
        assert store.is_delivery_processed("github", "d2") is True
        assert store.is_delivery_processed("github", "d3") is False

    def test_delivery_stores_event_type_and_action(
        self, store: SQLiteStore
    ) -> None:
        store.mark_delivery_processed("github", "delivery-xyz", "issue_comment", "created")
        row = store.conn.execute(
            "SELECT event_type, action FROM processed_deliveries WHERE delivery_id = ?",
            ("delivery-xyz",),
        ).fetchone()
        assert row is not None
        assert row["event_type"] == "issue_comment"
        assert row["action"] == "created"


class TestTaskUniqueConstraint:
    def test_duplicate_task_rejected(self, store: SQLiteStore) -> None:
        task = _make_task()
        store.insert_task(task)
        dup = _make_task()
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_task(dup)

    def test_different_trigger_kind_allowed(self, store: SQLiteStore) -> None:
        task = _make_task(delivery_id="d1", trigger_kind=TriggerKind.ISSUE_OPENED)
        store.insert_task(task)
        task2 = _make_task(
            delivery_id="d2", trigger_kind=TriggerKind.LABEL_ADDED
        )
        store.insert_task(task2)
        assert store.get_queue_depth() == 2

    def test_same_trigger_different_issue_allowed(
        self, store: SQLiteStore
    ) -> None:
        t1 = _make_task(delivery_id="d1", issue_number=1)
        store.insert_task(t1)
        t2 = _make_task(delivery_id="d2", issue_number=2)
        store.insert_task(t2)
        assert store.get_queue_depth() == 2

    def test_task_allowed_after_terminal_status(
        self, store: SQLiteStore
    ) -> None:
        t1 = _make_task(delivery_id="d1")
        store.insert_task(t1)
        store.update_status(t1.id, TaskStatus.SUCCEEDED)
        t2 = _make_task(delivery_id="d2")
        store.insert_task(t2)
        assert store.get_queue_depth() == 1

    def test_task_allowed_after_different_status(
        self, store: SQLiteStore
    ) -> None:
        t1 = _make_task(delivery_id="d1")
        store.insert_task(t1)
        store.update_status(t1.id, TaskStatus.FAILED_TERMINAL)
        t2 = _make_task(delivery_id="d2")
        store.insert_task(t2)
        assert store.get_queue_depth() == 1


class TestConcurrentSafety:
    def test_double_fetch_returns_same_task_once(
        self, store: SQLiteStore
    ) -> None:
        t1 = _make_task(delivery_id="d1", issue_number=1)
        t2 = _make_task(delivery_id="d2", issue_number=2)
        store.insert_task(t1)
        store.insert_task(t2)
        first = store.fetch_next()
        second = store.fetch_next()
        assert first is not None
        assert second is not None
        assert first.id != second.id
