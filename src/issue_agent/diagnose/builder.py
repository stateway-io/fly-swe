from __future__ import annotations

from typing import TYPE_CHECKING

from issue_agent.diagnose.verdict import DiagnosisVerdict, sanitize_rationale

if TYPE_CHECKING:
    from issue_agent.queue.models import Task

_CATEGORY_LABELS: dict[str, str] = {
    "question": "a question",
    "feature_request": "a feature request",
    "high_severity_bug": "a high-severity bug",
    "bug": "a (non-critical) bug",
    "docs": "a documentation issue",
    "chore": "a chore",
    "agent_fix_please": "a fix request",
}


def _describe(category: str | None) -> str:
    if category is None:
        return "unclassified"
    return _CATEGORY_LABELS.get(category, category)


def _extract_issue_info(task: Task) -> dict[str, str]:
    trigger = task.trigger
    return {
        "title": (trigger.issue.title if trigger else "") or "(no title)",
        "author": (trigger.actor.login if trigger else "") or "unknown",
    }


def build_diagnosis_comment(verdict: DiagnosisVerdict | None, task: Task, agent_name: str) -> str:
    """Build the (only) public output of a DIAGNOSE run.

    The body is assembled entirely from the two enum fields on `verdict`
    (`actual_category`, `confidence`) plus static labels from
    `_CATEGORY_LABELS` — that part can never contain agent-authored prose.
    The one piece of free text, `verdict.rationale`, is sanitized
    (diagnose/verdict.sanitize_rationale) before inclusion, and the whole
    "Why" section is omitted if nothing survives sanitization. There is no
    `submission`/free-text parameter here on purpose — a DIAGNOSE run's raw
    agent output must never reach this function.
    """
    info = _extract_issue_info(task)
    labeled_category = task.label_category
    header = f"## 🤖 {agent_name} — Diagnosis\n\nHi @{info['author']}!"

    if verdict is None:
        return (
            f"{header} I inspected the repository to check whether this issue's "
            f"classification ({_describe(labeled_category)}) matches reality, but "
            f"the diagnosis was inconclusive.\n\n"
            f"> **Issue:** {info['title']}\n\n"
            f"A human should take a look.\n\n"
            f"_This is an automated diagnosis. No code was changed._"
        )

    if verdict.actual_category == labeled_category:
        body = (
            f"{header} I inspected the repository — this issue's classification as "
            f"{_describe(labeled_category)} matches what I found (confidence: "
            f"{verdict.confidence}).\n\n"
            f"> **Issue:** {info['title']}"
        )
    else:
        body = (
            f"{header} I inspected the repository to check this issue's "
            f"classification.\n\n"
            f"> **Issue:** {info['title']}\n\n"
            f"It was labeled as **{_describe(labeled_category)}** "
            f"(`{labeled_category}`), but the diagnosis found it is actually "
            f"**{_describe(verdict.actual_category)}** (`{verdict.actual_category}`) "
            f"— confidence: {verdict.confidence}."
        )

    why = sanitize_rationale(verdict.rationale)
    if why:
        body += f"\n\n**Why:** {why}"

    body += "\n\n_This is an automated diagnosis. No code was changed."
    if verdict.actual_category != labeled_category:
        body += " A human should confirm and relabel if appropriate."
    body += "_"

    return body
