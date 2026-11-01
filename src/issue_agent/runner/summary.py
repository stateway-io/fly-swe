"""Block summaries for work resumption.

When a run blocks on a timeout or a budget/cost stop, the partial work is
pushed to the work branch and a short summary of what was done / what remains
is persisted on ``issue_work`` and injected into the next run's prompt. Two
sources, per ``resume.summary_on_block``:

- ``mechanical`` — ``git diff`` against HEAD plus the last few turns of the
  trajectory the agent already produced. Zero extra tokens, and the
  default.
- ``llm`` — one extra model call over the trajectory. Counted against the
  task cost and the daily budget like any other call.

An LLM summary that fails — including ``CostUnavailableError`` under strict
cost tracking, the expected outcome when the block itself was a cost stop —
degrades to the mechanical summary; the block is never left without one.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from issue_agent.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

_MECHANICAL = "mechanical"
_LLM = "llm"

# Blocks where an extra LLM call is exactly what must not happen under
# ``llm_unless_budget``.
_COST_BLOCKS = {"budget_exceeded", "cost_unavailable"}

_RECENT_ACTION_LIMIT = 6
_RECENT_ACTION_CHARS = 800
_TRANSCRIPT_MSG_LIMIT = 40
_TRANSCRIPT_MSG_CHARS = 2000
_TRUNCATION_MARKER = "\n[… summary truncated]"

_LLM_SUMMARY_SYSTEM = (
    "You are handing off an interrupted software task to the next attempt. "
    "Given the trajectory of the interrupted attempt below, write a concise "
    "note (at most ~250 words): what was investigated, what was already "
    "changed, what still needs doing, and any dead ends to avoid. The partial "
    "work is already committed on the branch. Output only the note."
)


def _git(ws_path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, cwd=ws_path)
    return result.stdout.strip() if result.returncode == 0 else ""


def _touched_files(ws_path: Path) -> str:
    lines: list[str] = []
    tracked = _git(ws_path, "diff", "--name-status", "HEAD")
    if tracked:
        lines.extend(tracked.splitlines())
    for f in _git(ws_path, "ls-files", "--others", "--exclude-standard").splitlines():
        lines.append(f"A\t{f}")
    return "\n".join(lines)


def _recent_actions(messages: list[dict[str, Any]]) -> str:
    turns = [
        (m.get("content") or "").strip()
        for m in messages
        if m.get("role") == "assistant" and (m.get("content") or "").strip()
    ]
    turns = turns[-_RECENT_ACTION_LIMIT:]
    return "\n\n".join(
        f"[recent step {i}]\n{t[:_RECENT_ACTION_CHARS]}" for i, t in enumerate(turns, 1)
    )


def build_mechanical_summary(ws_path: Path, messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    files = _touched_files(ws_path)
    if files:
        parts.append(f"Files changed so far:\n{files}")
    diffstat = _git(ws_path, "diff", "--stat", "HEAD")
    if diffstat:
        parts.append(f"Diff stat (vs. branch point):\n{diffstat}")
    actions = _recent_actions(messages)
    if actions:
        parts.append(f"Most recent actions in the interrupted attempt:\n{actions}")
    if not parts:
        return "No file changes were recorded before the run stopped."
    return "\n\n".join(parts)


def _render_transcript(messages: list[dict[str, Any]]) -> str:
    recent = messages[-_TRANSCRIPT_MSG_LIMIT:]
    out: list[str] = []
    for m in recent:
        role = m.get("role") or "?"
        if role == "system":
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        out.append(f"### {role}\n{content[:_TRANSCRIPT_MSG_CHARS]}")
    return "\n\n".join(out)


def build_llm_summary(model: Any, messages: list[dict[str, Any]]) -> tuple[str, float]:
    """One extra model call. Returns ``(text, cost_usd)``. Raises on failure —
    the caller degrades to the mechanical summary."""
    response = model.query(
        [
            {"role": "system", "content": _LLM_SUMMARY_SYSTEM},
            {"role": "user", "content": _render_transcript(messages)},
        ]
    )
    text = (response.get("content") or "").strip()
    cost = float(response.get("extra", {}).get("cost", 0.0) or 0.0)
    if not text:
        raise RuntimeError("LLM summary came back empty")
    return text, cost


def _truncate(text: str, max_chars: int) -> str:
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars].rstrip() + _TRUNCATION_MARKER
    return text


def summarize_block(
    *,
    policy: str,
    block_error: str,
    ws_path: Path,
    messages: list[dict[str, Any]],
    model: Any,
    max_chars: int,
) -> tuple[str, str, float]:
    """Produce the block summary. Returns ``(text, kind, extra_cost_usd)``
    where ``kind`` is ``'' | 'mechanical' | 'llm'`` — a policy that asked for
    an LLM summary but degraded reports ``'mechanical'``."""
    if policy == "never":
        return "", "", 0.0

    want_llm = policy == _LLM or (policy == "llm_unless_budget" and block_error not in _COST_BLOCKS)

    if want_llm:
        try:
            text, cost = build_llm_summary(model, messages)
            return _truncate(text, max_chars), _LLM, cost
        except Exception as exc:
            logger.warning("llm_block_summary_degraded", error=str(exc), block_error=block_error)

    return _truncate(build_mechanical_summary(ws_path, messages), max_chars), _MECHANICAL, 0.0
