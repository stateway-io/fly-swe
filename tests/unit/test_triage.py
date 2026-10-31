from __future__ import annotations

from typing import TYPE_CHECKING

from issue_agent.triage.builder import build_triage_comment
from tests.helpers.factories import make_author, make_issue, make_trigger_event

if TYPE_CHECKING:
    from issue_agent.core.models import TriggerEvent

_AGENT_NAME = "Test Agent"


def _payload(
    title: str = "Test issue",
    body: str = "Test body",
    author: str = "test-user",
) -> TriggerEvent:
    return make_trigger_event(
        issue=make_issue(number=1, title=title, body=body),
        actor=make_author(login=author),
    )


class TestBuildTriageComment:
    def test_question_category_contains_question_header(self) -> None:
        result = build_triage_comment("question", _payload(), _AGENT_NAME)
        assert "Triage" in result
        assert "question" in result.lower()

    def test_question_category_mentions_author(self) -> None:
        result = build_triage_comment(
            "question", _payload(author="joao"), _AGENT_NAME
        )
        assert "@joao" in result

    def test_question_category_mentions_title(self) -> None:
        result = build_triage_comment(
            "question", _payload(title="Como usar?"), _AGENT_NAME
        )
        assert "Como usar?" in result

    def test_feature_request_category_contains_feature_header(self) -> None:
        result = build_triage_comment("feature_request", _payload(), _AGENT_NAME)
        assert "Feature Request" in result
        assert "suggestion" in result

    def test_high_severity_bug_mentions_severity(self) -> None:
        result = build_triage_comment("high_severity_bug", _payload(), _AGENT_NAME)
        assert "high severity" in result.lower()

    def test_general_category_fallback(self) -> None:
        result = build_triage_comment("general", _payload(), _AGENT_NAME)
        assert "Triage" in result
        assert "received" in result.lower()

    def test_none_category_fallback_to_general(self) -> None:
        result = build_triage_comment(None, _payload(), _AGENT_NAME)
        assert "Triage" in result

    def test_with_empty_body(self) -> None:
        result = build_triage_comment("question", _payload(body=""), _AGENT_NAME)
        assert "question" in result.lower()

    def test_without_a_trigger(self) -> None:
        result = build_triage_comment("question", None, _AGENT_NAME)
        assert "Triage" in result
        assert "@unknown" in result
        assert "(no title)" in result

    def test_greets_the_actor_not_the_issue_author(self) -> None:
        trigger = make_trigger_event(
            issue=make_issue(author=make_author(login="reporter")),
            actor=make_author(login="labeler"),
        )
        result = build_triage_comment("question", trigger, _AGENT_NAME)
        assert "@labeler" in result

    def test_uses_configured_agent_name_in_every_category(self) -> None:
        for category in ("question", "feature_request", "high_severity_bug", "general", None):
            result = build_triage_comment(category, _payload(), "My Custom Bot")
            assert "My Custom Bot" in result
            assert "Stateway" not in result

    def test_team_reference_is_project_not_bot_name(self) -> None:
        # Regression: a prior fix-round mechanically replaced "Stateway"
        # with {agent_name} everywhere, including "team" sentences — which
        # produces nonsense like "reviewed by the my-custom-bot team" (the
        # bot doesn't have a "team", the project does). The bot's own name
        # legitimately stays in headers/signatures, but body sentences about
        # "the team" must refer to the project generically, never repeat
        # the configured agent name.
        for category in ("question", "feature_request", "high_severity_bug", "general", None):
            result = build_triage_comment(category, _payload(), "My Custom Bot")
            assert "My Custom Bot team" not in result
            assert "My Custom Bot's team" not in result
