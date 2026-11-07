"""Harness-side enforcement of ``repos[].project.protected_paths``.

The prompt already tells the agent not to touch these paths
(``prompts/system.md``), but an instruction is not a guarantee. The harness
owns git, so before every commit it stages the agent's work, finds the paths it
changed that match the configured patterns, and restores them to the state
they had before the agent ran. Everything else is committed as usual.

Pattern rules (paths are repo-relative, ``/``-separated):

- a pattern with a glob character (``*``, ``?``, ``[``) is matched against the
  whole path with :func:`fnmatch.fnmatchcase` (``*`` also crosses ``/``);
- otherwise it names a file or a directory: it matches that exact path or
  anything under it. ``migrations``, ``migrations/`` and ``./migrations/`` are
  the same pattern, and none of them matches ``migrations_old/x``.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

RunGit = Callable[..., str]

_GLOB_CHARS = frozenset("*?[")


def normalize(patterns: Iterable[str]) -> list[str]:
    """Strip whitespace and leading ``./`` / ``/``; drop empty entries."""
    normalized: list[str] = []
    for raw in patterns:
        pattern = raw.strip()
        while pattern.startswith("./"):
            pattern = pattern[2:]
        pattern = pattern.lstrip("/")
        if pattern and pattern not in normalized:
            normalized.append(pattern)
    return normalized


def is_protected(path: str, patterns: Iterable[str]) -> bool:
    """Whether the repo-relative ``path`` matches any of the (normalized) patterns."""
    for pattern in patterns:
        if _GLOB_CHARS.intersection(pattern):
            if fnmatch.fnmatchcase(path, pattern):
                return True
            continue
        root = pattern.rstrip("/")
        if root and (path == root or path.startswith(root + "/")):
            return True
    return False


def enforce(
    ws_path: Path,
    patterns: Iterable[str],
    source_sha: str,
    run_git: RunGit,
    task_log=None,
) -> list[str]:
    """Revert every change to a protected path since ``source_sha``.

    Stages the whole working tree, lists the paths that differ from
    ``source_sha`` (renames split into their delete + add sides, so moving a
    protected file away counts as touching it), and restores the protected
    ones — index and working tree — to ``source_sha``. A protected file the
    agent created is removed. Returns the reverted paths, sorted; with no
    patterns it does nothing and makes no git call.
    """
    normalized = normalize(patterns)
    if not normalized:
        return []

    run_git(ws_path, "add", "-A")
    output = run_git(ws_path, "diff", "--cached", "--name-only", "--no-renames", "-z", source_sha)
    changed = [p for p in output.split("\0") if p]
    reverted = sorted({p for p in changed if is_protected(p, normalized)})
    if not reverted:
        return []

    # Literal pathspecs: a file literally named `a*.sql` must not expand.
    run_git(
        ws_path,
        "restore",
        f"--source={source_sha}",
        "--staged",
        "--worktree",
        "--",
        *reverted,
        env={"GIT_LITERAL_PATHSPECS": "1"},
    )
    if task_log is not None:
        task_log.warning("protected_paths_reverted", paths=reverted)
    return reverted


def reverted_note(paths: list[str]) -> str:
    """The line shown to humans (issue comment, change-request body) when the
    harness reverted protected paths. Empty when nothing was reverted."""
    if not paths:
        return ""
    listed = ", ".join(f"`{p}`" for p in paths)
    return (
        "⚠️ The agent changed protected paths; the harness reverted those "
        f"changes before committing: {listed}."
    )
