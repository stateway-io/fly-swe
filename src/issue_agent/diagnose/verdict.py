from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from issue_agent.policy.label_router import ALL_CATEGORIES

if TYPE_CHECKING:
    from pathlib import Path

# Mirrors executor.py's TEST_COMMAND_SENTINEL_FILENAME pattern: the agent
# writes this file inside the workspace root during a DIAGNOSE run; the
# harness reads and deletes it right after the agent's run ends, before any
# comment is posted. This is the ONLY channel through which a DIAGNOSE run's
# result reaches GitHub — the agent's free-form `submission` text is never
# published (see diagnose/builder.py).
SENTINEL_FILENAME = ".agent-diagnosis.json"

_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})

_DEFAULT_MAX_RATIONALE_CHARS = 1200
_TRUNCATION_MARKER = "\n[…truncated]"

_FENCED_BLOCK_RE = re.compile(r"(```|~~~).*?\1", re.DOTALL)
_INDENTED_BLOCK_RE = re.compile(r"(?:^|\n)(?: {4}[^\n]*(?:\n|$))+")
_LONG_INLINE_CODE_RE = re.compile(r"`[^`\n]{40,}`")


@dataclass(frozen=True)
class DiagnosisVerdict:
    actual_category: str
    confidence: str
    rationale: str


def consume_sentinel(ws_path: Path) -> str | None:
    """Read the agent's ``.agent-diagnosis.json`` sentinel (if any) and
    delete it. Must run before any git bookkeeping / comment posting, same
    as ``executor._consume_test_command_sentinel``."""
    sentinel = ws_path / SENTINEL_FILENAME
    if not sentinel.is_file():
        return None

    try:
        content = sentinel.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        content = ""
    finally:
        sentinel.unlink(missing_ok=True)

    return content or None


def parse_verdict(raw: str | None) -> DiagnosisVerdict | None:
    """Validate the sentinel's content against the verdict contract.

    Anything outside the contract — malformed JSON, an unrecognized
    category/confidence, wrong field types — returns None rather than
    raising. A missing/invalid verdict is reported to the issue as an
    inconclusive diagnosis (builder.py), never as an error that leaks
    internals.
    """
    if raw is None:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    actual_category = data.get("actual_category")
    confidence = data.get("confidence")
    rationale = data.get("rationale", "")

    if not isinstance(actual_category, str) or actual_category not in ALL_CATEGORIES:
        return None
    if not isinstance(confidence, str) or confidence not in _VALID_CONFIDENCE:
        return None
    if not isinstance(rationale, str):
        return None

    return DiagnosisVerdict(
        actual_category=actual_category, confidence=confidence, rationale=rationale
    )


def sanitize_rationale(
    text: str, max_chars: int = _DEFAULT_MAX_RATIONALE_CHARS
) -> str:
    """Strip anything that looks like source code out of a model-authored
    rationale before it can reach a public GitHub comment.

    This is defense in depth, not the primary guarantee — the primary
    guarantee is that the comment body is otherwise built entirely from the
    two enum fields (see diagnose/builder.py). A determined model can still
    describe logic in prose; what this makes impossible is pasting an
    actual code/diff excerpt.
    """
    if not text:
        return ""

    cleaned = _FENCED_BLOCK_RE.sub("", text)
    cleaned = _INDENTED_BLOCK_RE.sub("\n", cleaned)
    cleaned = _LONG_INLINE_CODE_RE.sub("", cleaned)
    cleaned = cleaned.strip()

    if max_chars > 0 and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + _TRUNCATION_MARKER

    return cleaned
