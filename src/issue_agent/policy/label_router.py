from __future__ import annotations

from enum import StrEnum


class LabelAction(StrEnum):
    SKIP = "skip"
    TRIAGE_ONLY = "triage_only"
    DIAGNOSE = "diagnose"
    FIX = "fix"


LABEL_BUG = "bug"
LABEL_DOCS = "docs"
LABEL_CHORE = "chore"
LABEL_QUESTION = "question"
LABEL_FEATURE_REQUEST = "feature-request"
LABEL_AGENT_FIX_PLEASE = "agent-fix-please"
LABEL_AGENT_SKIP = "agent-skip"
LABEL_SEVERITY_HIGH = "severity:high"
LABEL_SEVERITY_CRITICAL = "severity:critical"

# Category identifiers — this set matches config.yaml's prompts.category_map
# keys exactly, and is what load_system_prompt()/build_triage_comment() key
# off of downstream.
CATEGORY_QUESTION = "question"
CATEGORY_FEATURE_REQUEST = "feature_request"
CATEGORY_HIGH_SEVERITY_BUG = "high_severity_bug"
CATEGORY_BUG = "bug"
CATEGORY_DOCS = "docs"
CATEGORY_CHORE = "chore"
CATEGORY_AGENT_FIX_PLEASE = "agent_fix_please"

# Full set of categories get_label_category() ever returns. Also the
# universe of valid entries in Settings.policy_triage_only_categories /
# policy_diagnose_categories — anything outside this set is a config typo
# (config.py's model_validator rejects it at boot).
ALL_CATEGORIES = frozenset(
    {
        CATEGORY_QUESTION,
        CATEGORY_FEATURE_REQUEST,
        CATEGORY_HIGH_SEVERITY_BUG,
        CATEGORY_BUG,
        CATEGORY_DOCS,
        CATEGORY_CHORE,
        CATEGORY_AGENT_FIX_PLEASE,
    }
)

# Default routing — identical to the hardcoded behaviour that shipped before
# these became configurable (config.py's Settings fields default to these).
DEFAULT_TRIAGE_ONLY_CATEGORIES = frozenset(
    {CATEGORY_QUESTION, CATEGORY_FEATURE_REQUEST, CATEGORY_HIGH_SEVERITY_BUG}
)
DEFAULT_DIAGNOSE_CATEGORIES: frozenset[str] = frozenset()

# Categories that route to LabelAction.FIX unless overridden into
# TRIAGE_ONLY/DIAGNOSE by config (see _classify_labels' precedence).
_FIX_CATEGORIES = frozenset(
    {CATEGORY_BUG, CATEGORY_DOCS, CATEGORY_CHORE, CATEGORY_AGENT_FIX_PLEASE}
)


def classify_labels(
    labels: list[str],
    triage_only_categories: frozenset[str] | None = None,
    diagnose_categories: frozenset[str] | None = None,
) -> LabelAction:
    """Route by the issue's *current* label set (``IssueRef.labels``)."""
    return _classify_labels(labels, triage_only_categories, diagnose_categories)


def _classify_labels(
    labels: list[str],
    triage_only_categories: frozenset[str] | None,
    diagnose_categories: frozenset[str] | None,
) -> LabelAction:
    normalized = {lb.strip().lower() for lb in labels if lb}

    if LABEL_AGENT_SKIP in normalized:
        return LabelAction.SKIP

    category = get_label_category(labels)
    if category is None:
        return LabelAction.SKIP

    triage_only = (
        DEFAULT_TRIAGE_ONLY_CATEGORIES
        if triage_only_categories is None
        else triage_only_categories
    )
    diagnose = (
        DEFAULT_DIAGNOSE_CATEGORIES if diagnose_categories is None else diagnose_categories
    )

    # diagnose is checked before triage_only so a category moved into
    # diagnose_categories (e.g. "bug", normally FIX) takes effect even if it
    # is also still listed as triage-only by mistake.
    if category in diagnose:
        return LabelAction.DIAGNOSE

    if category in triage_only:
        return LabelAction.TRIAGE_ONLY

    if category in _FIX_CATEGORIES:
        return LabelAction.FIX

    return LabelAction.SKIP



def labels_can_trigger(labels: list[str]) -> bool:
    """Whether a ``label_added`` event with this current label set is worth
    a task (PRD §3.1: ``label_added`` means "the label set changed").

    True when the set routes to a category, or carries ``agent-skip`` — the
    explicit opt-out still gets acknowledged on the issue. A set of only
    unrelated labels (``wontfix``, ``priority:low``…) is dropped at the
    webhook instead of becoming a task that aborts and posts "out of scope".
    """
    normalized = {lb.strip().lower() for lb in labels if lb}
    return LABEL_AGENT_SKIP in normalized or get_label_category(labels) is not None


def get_label_category(labels: list[str]) -> str | None:
    """Single source of truth for label -> prompt category resolution.

    The returned strings are exactly the keys used by config.yaml's
    ``prompts.category_map`` (and ``settings.prompt_category_map``), so the
    result can be assigned directly to ``Task.label_category`` and later fed
    to ``load_system_prompt()`` / ``build_triage_comment()`` unchanged.

    Precedence (first match wins) mirrors the routing priority previously
    encoded ad hoc in ``_classify_labels``: question > feature-request >
    bug (severity-qualified) > docs > chore > agent-fix-please > none.
    """
    normalized = {lb.strip().lower() for lb in labels if lb}

    if LABEL_QUESTION in normalized:
        return CATEGORY_QUESTION

    if LABEL_FEATURE_REQUEST in normalized:
        return CATEGORY_FEATURE_REQUEST

    if LABEL_BUG in normalized:
        if LABEL_SEVERITY_HIGH in normalized or LABEL_SEVERITY_CRITICAL in normalized:
            return CATEGORY_HIGH_SEVERITY_BUG
        return CATEGORY_BUG

    if LABEL_DOCS in normalized:
        return CATEGORY_DOCS

    if LABEL_CHORE in normalized:
        return CATEGORY_CHORE

    if LABEL_AGENT_FIX_PLEASE in normalized:
        return CATEGORY_AGENT_FIX_PLEASE

    return None
