from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from issue_agent.core.models import TriggerEvent


def build_triage_comment(
    category: str | None, trigger: TriggerEvent | None, agent_name: str
) -> str:
    title = (trigger.issue.title if trigger else "") or "(no title)"
    author = (trigger.actor.login if trigger else "") or "unknown"

    category = category or "general"

    if category == "question":
        return (
            f"## 🤖 {agent_name} — Triage\n\n"
            f"Hi @{author}! Thanks for the question.\n\n"
            f"> **Your question:** {title}\n\n"
            f"We received your question. It has been logged and will be reviewed by the project team. "
            f"In the meantime, you may want to check the project's documentation "
            f"to see if your question has already been answered.\n\n"
            f"_This is an automated triage reply._"
        )

    if category == "feature_request":
        return (
            f"## 🤖 {agent_name} — Feature Request Analysis\n\n"
            f"Hi @{author}! Thanks for the suggestion.\n\n"
            f"> **Feature request:** {title}\n\n"
            f"We received your suggestion. It has been logged for the team's review.\n\n"
            f"_This is an automated analysis. The decision to implement is up to the project team._"
        )

    if category == "high_severity_bug":
        return (
            f"## 🤖 {agent_name} — Bug Triage (High Severity)\n\n"
            f"Hi @{author}! Thanks for the report.\n\n"
            f"> **Reported bug:** {title}\n\n"
            f"This bug has been classified as **high severity** and will be prioritized by the team. "
            f"In the meantime, it would help to confirm:\n"
            f"- Complete error logs\n"
            f"- Steps to reproduce\n"
            f"- Software version in use\n\n"
            f"_Notification sent to the project team._"
        )

    return (
        f"## 🤖 {agent_name} — Triage\n\n"
        f"Hi @{author}! Thanks for reaching out.\n\n"
        f"> **Issue:** {title}\n\n"
        f"Your issue has been received and will be reviewed by the project team.\n\n"
        f"_This is an automated triage reply._"
    )
