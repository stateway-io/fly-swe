from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.observability.audit import read_audit_entries
from issue_agent.queue.models import TriggerKind
from issue_agent.queue.store import SQLiteStore
from issue_agent.webhook.routes import _handle_issue_lifecycle
from tests.helpers.factories import make_issue, make_trigger_event
from tests.helpers.settings import github_providers

if TYPE_CHECKING:
    from issue_agent.core.models import TriggerEvent


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


def _settings(data_dir: object) -> Settings:
    return SettingsNoEnv(
        PROVIDERS=github_providers(app_id=1, app_private_key="k", webhook_secret="s"),
        LLM_API_KEY="k",
        DATA_DIR=str(data_dir),
    )


@pytest.fixture
def store() -> SQLiteStore:
    s = SQLiteStore(":memory:")
    s.init()
    return s


def _parsed(trigger_kind: TriggerKind, repo: str = "owner/issues", n: int = 1) -> TriggerEvent:
    return make_trigger_event(trigger_kind, issue=make_issue(repo=repo, number=n))


class TestIssueClosedObservability:
    """Closing an ``issue_work`` via the
    ``issues.closed`` webhook — the primary close path — must leave the
    same audit/metric trail the executor-driven closes do."""

    def test_closing_open_work_writes_audit_and_observes_metric(
        self, store: SQLiteStore, tmp_path
    ) -> None:
        settings = _settings(tmp_path)
        store.open_issue_work("github", "owner/issues", 1, "owner/code", "main", "agent/issue-1-x")
        store.update_issue_work("github", "owner/issues", 1, attempts=2)

        with patch("issue_agent.webhook.routes.observe_issue_work_closed") as observe:
            status, reason = _handle_issue_lifecycle(
                store, settings, _parsed(TriggerKind.ISSUE_CLOSED)
            )

        assert (status, reason) == ("handled", "issue_work_closed")
        observe.assert_called_once_with("owner/code", 2, provider_instance="github")

        closed = [
            e for e in read_audit_entries(str(tmp_path)) if e.get("event") == "issue_work_closed"
        ]
        assert len(closed) == 1
        assert closed[0]["closed_reason"] == "issue_closed"
        assert closed[0]["attempts"] == 2
        assert closed[0]["work_branch"] == "agent/issue-1-x"
        assert closed[0]["issue_repo"] == "owner/issues"
        assert closed[0]["provider_instance"] == "github"
        assert closed[0]["issue_number"] == 1

    def test_closing_with_no_open_work_writes_nothing(self, store: SQLiteStore, tmp_path) -> None:
        settings = _settings(tmp_path)

        with patch("issue_agent.webhook.routes.observe_issue_work_closed") as observe:
            status, reason = _handle_issue_lifecycle(
                store, settings, _parsed(TriggerKind.ISSUE_CLOSED)
            )

        assert reason == "no_open_work"
        observe.assert_not_called()
        assert read_audit_entries(str(tmp_path)) == []


class TestReopenClearsIssueClosedBlock:
    """A row closed with ``issue_closed`` makes the closed-issue executor guard abort
    every future run on that issue. Reopening the issue means that close is no
    longer true, so the spent row must be dropped — a later comment then
    starts fresh work (§3.3)."""

    def test_reopen_drops_a_row_closed_by_issue_closed(self, store: SQLiteStore, tmp_path) -> None:
        settings = _settings(tmp_path)
        store.open_issue_work("github", "owner/issues", 1, "owner/code", "main", "agent/issue-1-x")
        store.close_issue_work("github", "owner/issues", 1, "issue_closed")

        status, reason = _handle_issue_lifecycle(
            store, settings, _parsed(TriggerKind.ISSUE_REOPENED)
        )

        assert (status, reason) == ("ignored", "issue_reopened")
        assert store.get_issue_work("github", "owner/issues", 1) is None
        dropped = [
            e
            for e in read_audit_entries(str(tmp_path))
            if e.get("event") == "issue_work_dropped_on_reopen"
        ]
        assert len(dropped) == 1
        assert dropped[0]["issue_number"] == 1

    def test_reopen_keeps_a_row_closed_for_another_reason(
        self, store: SQLiteStore, tmp_path
    ) -> None:
        settings = _settings(tmp_path)
        store.open_issue_work("github", "owner/issues", 1, "owner/code", "main", "agent/issue-1-x")
        store.close_issue_work("github", "owner/issues", 1, "max_attempts")

        _handle_issue_lifecycle(store, settings, _parsed(TriggerKind.ISSUE_REOPENED))

        work = store.get_issue_work("github", "owner/issues", 1)
        assert work is not None
        assert work.closed_reason == "max_attempts"

    def test_reopen_with_no_row_is_a_noop(self, store: SQLiteStore, tmp_path) -> None:
        settings = _settings(tmp_path)

        status, reason = _handle_issue_lifecycle(
            store, settings, _parsed(TriggerKind.ISSUE_REOPENED)
        )

        assert (status, reason) == ("ignored", "issue_reopened")
