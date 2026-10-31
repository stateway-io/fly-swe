from __future__ import annotations

from typing import TYPE_CHECKING

from issue_agent.diagnose.builder import build_diagnosis_comment
from issue_agent.diagnose.verdict import DiagnosisVerdict
from tests.helpers.factories import make_author, make_issue, make_task, make_trigger_event

if TYPE_CHECKING:
    from issue_agent.queue.models import Task

_AGENT_NAME = "Test Agent"


def _make_task(label_category: str | None, **overrides) -> Task:
    trigger = make_trigger_event(
        issue=make_issue(repo="owner/repo", number=7, title="Something is wrong", body="..."),
        actor=make_author(login="reporter"),
    )
    return make_task(trigger, label_category=label_category, **overrides)


class TestBuildDiagnosisComment:
    def test_matching_verdict_confirms_classification(self) -> None:
        task = _make_task("bug")
        verdict = DiagnosisVerdict(
            actual_category="bug", confidence="high", rationale="Confirmed reproduction."
        )
        result = build_diagnosis_comment(verdict, task, _AGENT_NAME)
        assert "matches" in result.lower() or "confirm" in result.lower()
        assert "bug" in result

    def test_diverging_verdict_reports_both_categories(self) -> None:
        task = _make_task("high_severity_bug")
        verdict = DiagnosisVerdict(
            actual_category="bug",
            confidence="medium",
            rationale="Low severity: only affects an edge case.",
        )
        result = build_diagnosis_comment(verdict, task, _AGENT_NAME)
        assert "high_severity_bug" in result or "high severity" in result.lower()
        assert "bug" in result
        assert "medium" in result.lower()

    def test_feature_request_reclassified_as_bug(self) -> None:
        task = _make_task("feature_request")
        verdict = DiagnosisVerdict(
            actual_category="bug",
            confidence="high",
            rationale="This is broken behavior, not a request for something new.",
        )
        result = build_diagnosis_comment(verdict, task, _AGENT_NAME)
        assert "feature_request" in result or "feature request" in result.lower()
        assert "bug" in result

    def test_none_verdict_is_inconclusive(self) -> None:
        task = _make_task("bug")
        result = build_diagnosis_comment(None, task, _AGENT_NAME)
        assert "inconclusive" in result.lower()

    def test_rationale_is_sanitized_before_inclusion(self) -> None:
        task = _make_task("bug")
        verdict = DiagnosisVerdict(
            actual_category="bug",
            confidence="high",
            rationale="It's here:\n```python\nSECRET = 'leaked'\n```\ndone.",
        )
        result = build_diagnosis_comment(verdict, task, _AGENT_NAME)
        assert "SECRET" not in result
        assert "```" not in result

    def test_mentions_agent_name(self) -> None:
        task = _make_task("bug")
        verdict = DiagnosisVerdict(actual_category="bug", confidence="high", rationale="")
        result = build_diagnosis_comment(verdict, task, "My Custom Bot")
        assert "My Custom Bot" in result

    def test_mentions_issue_author(self) -> None:
        task = _make_task("bug")
        verdict = DiagnosisVerdict(actual_category="bug", confidence="high", rationale="")
        result = build_diagnosis_comment(verdict, task, _AGENT_NAME)
        assert "@reporter" in result

    def test_empty_rationale_omits_why_section_without_crashing(self) -> None:
        task = _make_task("bug")
        verdict = DiagnosisVerdict(actual_category="docs", confidence="low", rationale="")
        result = build_diagnosis_comment(verdict, task, _AGENT_NAME)
        assert "docs" in result

    def test_never_includes_raw_submission_text(self) -> None:
        # build_diagnosis_comment must not accept/leak anything beyond the
        # verdict's own (sanitized) fields — no submission/free-text param.
        import inspect

        sig = inspect.signature(build_diagnosis_comment)
        assert "submission" not in sig.parameters


class TestWithoutTrigger:
    def test_missing_trigger_falls_back_instead_of_raising(self) -> None:
        # The old payload-based builder raised KeyError here.
        task = _make_task("bug", trigger_json=None)
        result = build_diagnosis_comment(None, task, _AGENT_NAME)
        assert "@unknown" in result
        assert "(no title)" in result
