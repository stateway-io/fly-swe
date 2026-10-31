from __future__ import annotations

from typing import Any

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.core.models import TriggerEvent, TriggerKind
from issue_agent.policy.ingestion import ingestion_filter
from tests.helpers.factories import (
    make_author,
    make_comment,
    make_issue,
    make_trigger_event,
)
from tests.helpers.settings import github_providers


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file=None, env_prefix="", extra="ignore", yaml_file=None,
    )


def _settings(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "PROVIDERS": github_providers(app_id=1, app_private_key="k", webhook_secret="s"),
        "LLM_API_KEY": "k",
    }
    fields.update(overrides)
    return SettingsNoEnv(**fields)


def _comment_event(
    *,
    login: str = "maintainer",
    association: str = "member",
    is_self: bool = False,
    target_kind: str = "issue",
    state: str | None = "open",
) -> TriggerEvent:
    return make_trigger_event(
        TriggerKind.COMMENT_COMMAND,
        target_kind=target_kind,
        issue=make_issue(author=make_author(login="reporter"), state=state),
        trigger_comment=make_comment(
            author=make_author(login=login, association=association, is_self=is_self)
        ),
    )


def _filter(trigger: TriggerEvent, **settings: Any) -> str | None:
    return ingestion_filter(trigger, _settings(**settings))


class TestCommentFilters:
    def test_allowed_comment_passes(self):
        assert _filter(_comment_event()) is None

    def test_comment_on_pull_request_is_dropped(self):
        assert _filter(_comment_event(target_kind="pr")) == "comment_on_pull_request"

    def test_pull_request_check_runs_before_own_bot_check(self):
        trigger = _comment_event(target_kind="pr", is_self=True)

        assert _filter(trigger) == "comment_on_pull_request"

    def test_own_bot_comment_is_dropped(self):
        assert _filter(_comment_event(is_self=True)) == "own_bot_comment"

    def test_own_bot_check_runs_before_closed_check(self):
        trigger = _comment_event(is_self=True, state="closed")

        assert _filter(trigger) == "own_bot_comment"

    def test_comment_on_closed_issue_is_dropped(self):
        assert _filter(_comment_event(state="closed")) == "issue_closed"

    def test_unknown_issue_state_is_not_treated_as_closed(self):
        assert _filter(_comment_event(state=None)) is None

    def test_author_not_allowed_is_dropped(self):
        trigger = _comment_event(login="stranger", association="none")

        assert _filter(trigger) == "comment_author_not_allowed"

    def test_issue_author_is_allowed(self):
        assert _filter(_comment_event(login="reporter", association="none")) is None

    def test_disabled_filter_lets_anyone_through(self):
        trigger = _comment_event(login="stranger", association="none")

        assert _filter(trigger, COMMENTS_AUTHOR_FILTER_ENABLED=False) is None

    def test_unknown_association_is_deferred_to_the_worker(self):
        # Can't be judged without a network call: enqueued, and the worker's
        # check_comment_author_allowed guard decides (PRD §3.1).
        trigger = _comment_event(login="stranger", association="unknown")

        assert _filter(trigger) is None


class TestLabelFilter:
    @pytest.mark.parametrize("labels", [[], ["wontfix"], ["priority:low"]])
    def test_label_set_without_trigger_label_is_dropped(self, labels):
        trigger = make_trigger_event(
            TriggerKind.LABEL_ADDED, issue=make_issue(labels=labels)
        )

        assert _filter(trigger) == "no_trigger_label"

    @pytest.mark.parametrize("labels", [["bug"], ["wontfix", "bug"], ["agent-skip"]])
    def test_label_set_with_trigger_label_passes(self, labels):
        trigger = make_trigger_event(
            TriggerKind.LABEL_ADDED, issue=make_issue(labels=labels)
        )

        assert _filter(trigger) is None

    def test_issue_opened_without_labels_is_not_filtered(self):
        trigger = make_trigger_event(
            TriggerKind.ISSUE_OPENED, issue=make_issue(labels=[])
        )

        assert _filter(trigger) is None


class TestOtherKinds:
    @pytest.mark.parametrize("kind", [TriggerKind.ISSUE_CLOSED, TriggerKind.ISSUE_REOPENED])
    def test_lifecycle_events_pass(self, kind):
        assert _filter(make_trigger_event(kind, issue=make_issue(state="closed"))) is None
