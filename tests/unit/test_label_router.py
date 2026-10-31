from __future__ import annotations

from issue_agent.policy.label_router import (
    LabelAction,
    classify_labels,
    get_label_category,
    labels_can_trigger,
)


class TestClassifyLabels:
    def test_agent_skip_overrides_everything(self) -> None:
        assert classify_labels(["agent-skip", "bug"]) == LabelAction.SKIP
        assert classify_labels(["agent-skip", "docs"]) == LabelAction.SKIP
        assert classify_labels(["agent-skip", "question"]) == LabelAction.SKIP
        assert classify_labels(["agent-skip", "bug", "severity:high"]) == LabelAction.SKIP
        assert classify_labels(["agent-skip", "agent-fix-please"]) == LabelAction.SKIP

    def test_question_returns_triage_only(self) -> None:
        assert classify_labels(["question"]) == LabelAction.TRIAGE_ONLY

    def test_question_with_other_labels_returns_triage_only(self) -> None:
        assert classify_labels(["question", "docs"]) == LabelAction.TRIAGE_ONLY
        assert classify_labels(["question", "chore"]) == LabelAction.TRIAGE_ONLY

    def test_feature_request_returns_triage_only(self) -> None:
        assert classify_labels(["feature-request"]) == LabelAction.TRIAGE_ONLY

    def test_feature_request_with_other_labels_returns_triage_only(self) -> None:
        assert classify_labels(["feature-request", "docs"]) == LabelAction.TRIAGE_ONLY

    def test_bug_no_severity_returns_fix(self) -> None:
        assert classify_labels(["bug"]) == LabelAction.FIX

    def test_bug_with_severity_low_returns_fix(self) -> None:
        assert classify_labels(["bug", "severity:low"]) == LabelAction.FIX

    def test_bug_with_severity_high_returns_triage_only(self) -> None:
        assert classify_labels(["bug", "severity:high"]) == LabelAction.TRIAGE_ONLY

    def test_bug_with_severity_critical_returns_triage_only(self) -> None:
        assert classify_labels(["bug", "severity:critical"]) == LabelAction.TRIAGE_ONLY

    def test_docs_returns_fix(self) -> None:
        assert classify_labels(["docs"]) == LabelAction.FIX

    def test_docs_with_other_labels_returns_fix(self) -> None:
        assert classify_labels(["docs", "chore"]) == LabelAction.FIX

    def test_chore_returns_fix(self) -> None:
        assert classify_labels(["chore"]) == LabelAction.FIX

    def test_agent_fix_please_converts_skip_to_fix(self) -> None:
        assert classify_labels(["agent-fix-please"]) == LabelAction.FIX

    def test_agent_fix_please_with_bug_returns_fix(self) -> None:
        assert classify_labels(["agent-fix-please", "bug"]) == LabelAction.FIX

    def test_empty_labels_returns_skip(self) -> None:
        assert classify_labels([]) == LabelAction.SKIP

    def test_unknown_labels_returns_skip(self) -> None:
        assert classify_labels(["random-label"]) == LabelAction.SKIP
        assert classify_labels(["enhancement", "help-wanted"]) == LabelAction.SKIP

    def test_none_in_list_ignored(self) -> None:
        assert classify_labels(["bug", None]) == LabelAction.FIX  # type: ignore[list-item]

    def test_empty_string_labels_ignored(self) -> None:
        assert classify_labels(["", "bug"]) == LabelAction.FIX

    def test_case_insensitivity(self) -> None:
        assert classify_labels(["BUG"]) == LabelAction.FIX
        assert classify_labels(["Bug"]) == LabelAction.FIX
        assert classify_labels(["DOCS"]) == LabelAction.FIX
        assert classify_labels(["Question"]) == LabelAction.TRIAGE_ONLY
        assert classify_labels(["AGENT-SKIP", "BUG"]) == LabelAction.SKIP

    def test_leading_trailing_whitespace(self) -> None:
        assert classify_labels(["  bug  "]) == LabelAction.FIX
        assert classify_labels(["  severity:high  ", "  bug  "]) == LabelAction.TRIAGE_ONLY

    def test_bug_priority_over_docs(self) -> None:
        assert classify_labels(["docs", "bug"]) == LabelAction.FIX

    def test_bug_priority_over_chore(self) -> None:
        assert classify_labels(["chore", "bug"]) == LabelAction.FIX

    def test_docs_priority_over_chore(self) -> None:
        assert classify_labels(["docs", "chore"]) == LabelAction.FIX

    def test_question_priority_over_bug(self) -> None:
        assert classify_labels(["question", "bug"]) == LabelAction.TRIAGE_ONLY
        assert classify_labels(["question", "docs"]) == LabelAction.TRIAGE_ONLY

    def test_feature_priority_over_bug(self) -> None:
        assert classify_labels(["feature-request", "bug"]) == LabelAction.TRIAGE_ONLY


class TestClassifyLabelsConfigurableCategories:
    """Categories routed to TRIAGE_ONLY/DIAGNOSE are configurable (see
    Settings.policy_triage_only_categories / policy_diagnose_categories);
    omitting the kwargs must reproduce the hardcoded defaults exactly."""

    def test_omitted_kwargs_match_hardcoded_defaults(self) -> None:
        assert classify_labels(["question"]) == LabelAction.TRIAGE_ONLY
        assert classify_labels(["bug"]) == LabelAction.FIX
        assert classify_labels([]) == LabelAction.SKIP

    def test_custom_diagnose_categories_overrides_fix(self) -> None:
        assert (
            classify_labels(["bug"], diagnose_categories=frozenset({"bug"}))
            == LabelAction.DIAGNOSE
        )

    def test_custom_diagnose_categories_overrides_triage_only(self) -> None:
        assert (
            classify_labels(
                ["question"], diagnose_categories=frozenset({"question"})
            )
            == LabelAction.DIAGNOSE
        )

    def test_empty_triage_only_categories_falls_through_to_skip(self) -> None:
        assert (
            classify_labels(["question"], triage_only_categories=frozenset())
            == LabelAction.SKIP
        )

    def test_custom_triage_only_categories_promotes_a_fix_category(self) -> None:
        assert (
            classify_labels(["docs"], triage_only_categories=frozenset({"docs"}))
            == LabelAction.TRIAGE_ONLY
        )

    def test_agent_skip_still_overrides_diagnose(self) -> None:
        assert (
            classify_labels(
                ["agent-skip", "bug"], diagnose_categories=frozenset({"bug"})
            )
            == LabelAction.SKIP
        )

    def test_no_recognized_category_is_skip_regardless_of_config(self) -> None:
        assert (
            classify_labels(["random"], diagnose_categories=frozenset({"random"}))
            == LabelAction.SKIP
        )


class TestGetLabelCategory:
    """Unified label -> category resolution (A7 / R5.8.2).

    This is the single source of truth consumed by both `_classify_labels`
    (FIX/TRIAGE_ONLY/SKIP routing) and `Task.label_category` (which feeds
    `load_system_prompt`/`build_triage_comment`). It must cover the full set
    of categories in config.yaml's `prompts.category_map`: bug, docs, chore,
    question, feature_request, high_severity_bug, agent_fix_please.
    """

    def test_question(self) -> None:
        assert get_label_category(["question"]) == "question"

    def test_feature_request(self) -> None:
        assert get_label_category(["feature-request"]) == "feature_request"

    def test_high_severity_bug(self) -> None:
        assert get_label_category(["bug", "severity:high"]) == "high_severity_bug"

    def test_critical_severity_bug(self) -> None:
        assert get_label_category(["bug", "severity:critical"]) == "high_severity_bug"

    def test_bug_low_severity_returns_bug(self) -> None:
        assert get_label_category(["bug", "severity:low"]) == "bug"

    def test_bug_no_severity_returns_bug(self) -> None:
        assert get_label_category(["bug"]) == "bug"

    def test_docs_returns_docs(self) -> None:
        assert get_label_category(["docs"]) == "docs"

    def test_chore_returns_chore(self) -> None:
        assert get_label_category(["chore"]) == "chore"

    def test_agent_fix_please_returns_agent_fix_please(self) -> None:
        assert get_label_category(["agent-fix-please"]) == "agent_fix_please"

    def test_agent_skip_returns_none(self) -> None:
        assert get_label_category(["agent-skip"]) is None

    def test_empty_labels(self) -> None:
        assert get_label_category([]) is None

    def test_unknown_labels(self) -> None:
        assert get_label_category(["random"]) is None

    def test_case_insensitivity(self) -> None:
        assert get_label_category(["BUG"]) == "bug"
        assert get_label_category(["DOCS"]) == "docs"
        assert get_label_category(["Question"]) == "question"

    def test_whitespace_insensitivity(self) -> None:
        assert get_label_category(["  bug  "]) == "bug"
        assert get_label_category(["  severity:high  ", "  bug  "]) == "high_severity_bug"

    # Precedence — migrated from the now-removed
    # runner/prompts.py::category_from_labels dead code (which covered the
    # full category set but was never wired up in production).
    def test_agent_fix_please_does_not_override_docs(self) -> None:
        assert get_label_category(["agent-fix-please", "docs"]) == "docs"

    def test_agent_fix_please_does_not_override_question(self) -> None:
        assert get_label_category(["agent-fix-please", "question"]) == "question"

    def test_bug_takes_priority_over_chore(self) -> None:
        assert get_label_category(["chore", "bug"]) == "bug"

    def test_bug_takes_priority_over_docs(self) -> None:
        assert get_label_category(["docs", "bug"]) == "bug"

    def test_docs_takes_priority_over_chore(self) -> None:
        assert get_label_category(["docs", "chore"]) == "docs"

    def test_question_takes_priority_over_bug(self) -> None:
        assert get_label_category(["question", "bug"]) == "question"

    def test_question_takes_priority_over_docs(self) -> None:
        assert get_label_category(["question", "docs"]) == "question"

    def test_feature_request_takes_priority_over_bug(self) -> None:
        assert get_label_category(["feature-request", "bug"]) == "feature_request"


class TestLabelsCanTrigger:
    def test_category_label_triggers(self):
        assert labels_can_trigger(["bug"]) is True

    def test_agent_skip_triggers_its_acknowledgement(self):
        assert labels_can_trigger(["Agent-Skip"]) is True

    def test_unrelated_labels_do_not_trigger(self):
        assert labels_can_trigger(["wontfix", "priority:low"]) is False

    def test_empty_set_does_not_trigger(self):
        assert labels_can_trigger([]) is False
