from __future__ import annotations

import pytest
from pydantic import ValidationError

from issue_agent.core.models import Author, TriggerEvent, TriggerKind
from issue_agent.queue.models import TriggerKind as QueueTriggerKind
from tests.helpers.factories import (
    make_author,
    make_comment,
    make_issue,
    make_trigger_event,
)


class TestTriggerEvent:
    def test_json_roundtrip(self):
        event = make_trigger_event(TriggerKind.COMMENT_COMMAND)

        restored = TriggerEvent.model_validate_json(event.model_dump_json())

        assert restored == event
        assert restored.trigger_comment is not None
        assert restored.trigger_comment.created_at is not None

    def test_actor_defaults_to_comment_author_in_factory(self):
        event = make_trigger_event(TriggerKind.COMMENT_COMMAND)

        assert event.trigger_comment is not None
        assert event.actor == event.trigger_comment.author

    def test_is_immutable(self):
        event = make_trigger_event()

        with pytest.raises(ValidationError):
            event.delivery_key = "other"  # type: ignore[misc]

    def test_rejects_unknown_target_kind(self):
        with pytest.raises(ValidationError):
            make_trigger_event(target_kind="merge_request")

    def test_comment_created_at_is_optional(self):
        assert make_comment(created_at=None).created_at is None

    def test_issue_repo_is_opaque(self):
        # GitLab-style nested groups must survive untouched.
        issue = make_issue(repo="group/sub/project")

        assert issue.repo == "group/sub/project"


class TestAuthor:
    def test_defaults_to_unknown_association(self):
        assert Author(login="x").association == "unknown"

    def test_rejects_unknown_association(self):
        with pytest.raises(ValidationError):
            make_author(association="OWNER")


def test_queue_models_reexport_the_same_trigger_kind():
    assert QueueTriggerKind is TriggerKind
