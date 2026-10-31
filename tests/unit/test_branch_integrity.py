"""Real-git regression tests for issue #86: a resumed work branch must never
end up with the old tree under the new base tip."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from issue_agent.runner.executor import (
    BaseUpdate,
    BranchIntegrityError,
    _abandon_base_merge,
    _assert_branch_integrity,
    _changed_files_vs_base,
    _commits_ahead_of_base,
    _finish_base_merge,
    _salvage_agent_commits,
    _update_branch_with_base,
)

if TYPE_CHECKING:
    from pathlib import Path


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_file(cwd: Path, name: str, content: str, msg: str) -> None:
    (cwd / name).write_text(content)
    git(cwd, "add", "-A")
    git(cwd, "commit", "-m", msg)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Clone whose `origin/main` has advanced past the work branch's base."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    ws = tmp_path / "ws"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", str(origin), str(seed))
    for r in (seed,):
        git(r, "config", "user.email", "t@t")
        git(r, "config", "user.name", "t")
    commit_file(seed, "a.txt", "a\n", "base")
    git(seed, "push", "origin", "main")
    # Work branch cut from the old base, with one change of its own.
    git(seed, "checkout", "-b", "work")
    commit_file(seed, "fix.txt", "fix\n", "fix")
    git(seed, "push", "origin", "work")
    # Base moves on.
    git(seed, "checkout", "main")
    commit_file(seed, "newfeature.txt", "feature\n", "base advances")
    git(seed, "push", "origin", "main")

    git(tmp_path, "clone", "-b", "main", str(origin), str(ws))
    git(ws, "config", "user.email", "t@t")
    git(ws, "config", "user.name", "t")
    git(ws, "fetch", "origin", "work")
    git(ws, "checkout", "-b", "work", "FETCH_HEAD")
    return ws


def test_salvage_resets_to_start_not_current_base(repo: Path) -> None:
    start = git(repo, "rev-parse", "HEAD")
    commit_file(repo, "agent.txt", "agent\n", "agent self-commit")

    status = _salvage_agent_commits(repo, "work", start, MagicMock())
    assert "agent.txt" in status
    # Only the agent's file is pending; nothing from the advanced base is
    # staged as a deletion.
    assert "newfeature.txt" not in status

    git(repo, "commit", "-m", "harness")
    assert (repo / "fix.txt").exists()
    # The branch keeps its own parent chain; nothing was re-parented onto the
    # new base tip.
    assert git(repo, "rev-parse", "HEAD~1") == start


def test_update_merges_base_and_integrity_passes(repo: Path) -> None:
    _update_branch_with_base(repo, "main", MagicMock())
    start = git(repo, "rev-parse", "HEAD")
    assert (repo / "newfeature.txt").exists()
    assert (repo / "fix.txt").exists()
    commit_file(repo, "more.txt", "more\n", "session change")
    _assert_branch_integrity(repo, "main", start)


def test_update_conflict_stays_in_progress_for_the_agent(repo: Path) -> None:
    commit_file(repo, "newfeature.txt", "conflicting\n", "conflicting change")
    before = git(repo, "rev-parse", "HEAD")
    log = MagicMock()
    update = _update_branch_with_base(repo, "main", log)
    assert update.conflicted
    assert update.conflicted_files == [("newfeature.txt", "both added")]
    # HEAD has not moved, but the merge is still open with its markers in
    # the working tree — the agent resolves them, the harness records it.
    assert git(repo, "rev-parse", "HEAD") == before
    git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    assert "<<<<<<<" in (repo / "newfeature.txt").read_text()
    log.warning.assert_called_once()


def test_clean_update_reports_merged(repo: Path) -> None:
    assert _update_branch_with_base(repo, "main", MagicMock()).status == "merged"


def test_integrity_rejects_old_tree_under_new_parent(repo: Path) -> None:
    """The exact signature from the issue: reset --soft to the new base tip,
    then commit — parent is base, tree is the old snapshot."""
    start = git(repo, "rev-parse", "HEAD")
    git(repo, "reset", "--soft", "origin/main")
    git(repo, "commit", "-m", "broken update")
    with pytest.raises(BranchIntegrityError, match="newfeature.txt"):
        _assert_branch_integrity(repo, "main", start)


def test_integrity_skips_when_not_based_on_base_tip(repo: Path) -> None:
    # Branch still on the old base: nothing to compare, must not raise.
    _assert_branch_integrity(repo, "main", git(repo, "rev-parse", "HEAD"))


# ── issue #89: deciding whether a resumed branch already carries work ──


def test_resumed_branch_reports_its_own_commits_and_files(repo: Path) -> None:
    """The run that produces nothing new still has to see the commit the
    branch already carries — otherwise it aborts as `no_changes` and the
    work is stranded with no PR (issue #89)."""
    assert _commits_ahead_of_base(repo, "main") == 1
    assert _changed_files_vs_base(repo, "main") == [("fix.txt", "added")]


def test_base_merge_does_not_pollute_the_branch_file_list(repo: Path) -> None:
    """After merging the base in, the three-dot diff must still show only
    the branch's own change — not everything the base gained."""
    _update_branch_with_base(repo, "main", MagicMock())

    assert _commits_ahead_of_base(repo, "main") == 2  # the fix + the merge
    assert _changed_files_vs_base(repo, "main") == [("fix.txt", "added")]


def test_branch_level_with_base_is_not_ahead(repo: Path) -> None:
    git(repo, "reset", "--hard", "origin/main")

    assert _commits_ahead_of_base(repo, "main") == 0
    assert _changed_files_vs_base(repo, "main") == []


def test_squash_merged_branch_is_ahead_but_has_no_content(repo: Path) -> None:
    """A branch whose work was squash-merged into the base stays "ahead" by
    the merge commit alone. Commit count is therefore not enough to justify
    a PR — the file list is what the caller must require."""
    git(repo, "checkout", "-b", "squashed", "origin/main")
    git(repo, "merge", "--squash", "work")
    git(repo, "commit", "-m", "squashed fix")
    git(repo, "push", "origin", "squashed:main")
    git(repo, "fetch", "origin", "main")
    git(repo, "checkout", "work")
    _update_branch_with_base(repo, "main", MagicMock())

    assert _commits_ahead_of_base(repo, "main") > 0
    assert _changed_files_vs_base(repo, "main") == []


def test_changed_files_maps_adds_edits_and_deletions(repo: Path) -> None:
    _update_branch_with_base(repo, "main", MagicMock())
    (repo / "newfeature.txt").write_text("edited\n")
    (repo / "a.txt").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "edit and delete")

    assert sorted(_changed_files_vs_base(repo, "main")) == [
        ("a.txt", "deleted"),
        ("fix.txt", "added"),
        ("newfeature.txt", "modified"),
    ]


# ── A conflicted base merge is resolved, never hidden ──


@pytest.fixture
def conflicted(tmp_path: Path) -> tuple[Path, str]:
    """Resumed work branch whose base merge stopped on a conflict in `a.txt`
    (both sides edited it) while `b.txt`, new on the base, merged cleanly.
    Returns the workspace and the branch's tip before the merge."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    ws = tmp_path / "ws"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", str(origin), str(seed))
    git(seed, "config", "user.email", "t@t")
    git(seed, "config", "user.name", "t")
    commit_file(seed, "a.txt", "a\n", "base")
    git(seed, "push", "origin", "main")
    git(seed, "checkout", "-b", "work")
    commit_file(seed, "fix.txt", "fix\n", "fix")
    commit_file(seed, "a.txt", "work\n", "work edits a")
    git(seed, "push", "origin", "work")
    git(seed, "checkout", "main")
    commit_file(seed, "a.txt", "base\n", "base edits a")
    commit_file(seed, "b.txt", "b\n", "base adds b")
    git(seed, "push", "origin", "main")

    git(tmp_path, "clone", "-b", "main", str(origin), str(ws))
    git(ws, "config", "user.email", "t@t")
    git(ws, "config", "user.name", "t")
    git(ws, "fetch", "origin", "work")
    git(ws, "checkout", "-b", "work", "FETCH_HEAD")
    start = git(ws, "rev-parse", "HEAD")
    update = _update_branch_with_base(ws, "main", MagicMock())
    assert update.conflicted_files == [("a.txt", "both modified")]
    return ws, start


_A_CONFLICT = BaseUpdate("conflicted", [("a.txt", "both modified")])


def test_resolved_merge_is_recorded_with_two_parents(conflicted) -> None:
    ws, start = conflicted
    (ws / "a.txt").write_text("work and base\n")
    (ws / "extra.txt").write_text("unrelated edit\n")

    assert _finish_base_merge(ws, "work", "main", start, _A_CONFLICT, MagicMock()) == []

    parents = git(ws, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    assert parents == [start, git(ws, "rev-parse", "origin/main")]
    assert git(ws, "rev-parse", "--abbrev-ref", "HEAD") == "work"
    assert not (ws / ".git" / "MERGE_HEAD").exists()
    # The merge commit holds the resolution and what the base brought in;
    # the agent's other edit is left for the harness's own commit on top.
    assert git(ws, "show", "HEAD:a.txt") == "work and base"
    assert git(ws, "show", "HEAD:b.txt") == "b"
    assert git(ws, "status", "--porcelain") == "?? extra.txt"

    git(ws, "add", "-A")
    git(ws, "commit", "-m", "harness")
    _assert_branch_integrity(ws, "main", start)
    assert sorted(_changed_files_vs_base(ws, "main")) == [
        ("a.txt", "modified"),
        ("extra.txt", "added"),
        ("fix.txt", "added"),
    ]


def test_markers_left_behind_block_the_merge(conflicted) -> None:
    ws, start = conflicted

    assert _finish_base_merge(ws, "work", "main", start, _A_CONFLICT, MagicMock()) == ["a.txt"]
    assert git(ws, "rev-parse", "HEAD") == start

    _abandon_base_merge(ws, start)
    assert git(ws, "rev-parse", "HEAD") == start
    assert git(ws, "status", "--porcelain") == ""
    assert (ws / "a.txt").read_text() == "work\n"


def test_setext_underline_is_not_a_conflict_marker(conflicted) -> None:
    ws, start = conflicted
    (ws / "a.txt").write_text("Title\n=======\n")

    assert _finish_base_merge(ws, "work", "main", start, _A_CONFLICT, MagicMock()) == []


def test_agent_committing_the_merge_still_yields_one_two_parent_merge(conflicted) -> None:
    ws, start = conflicted
    (ws / "a.txt").write_text("resolved\n")
    git(ws, "add", "-A")
    git(ws, "commit", "-m", "agent concluded the merge itself")

    assert _finish_base_merge(ws, "work", "main", start, _A_CONFLICT, MagicMock()) == []

    assert git(ws, "rev-list", "--count", f"{start}..HEAD") != "0"
    parents = git(ws, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    assert parents == [start, git(ws, "rev-parse", "origin/main")]
    assert git(ws, "show", "HEAD:a.txt") == "resolved"
    assert git(ws, "status", "--porcelain") == ""


def test_agent_aborting_the_merge_is_caught_by_integrity(conflicted) -> None:
    """`git merge --abort` puts the old tree back without markers: recorded
    as a merge, it would silently revert what the base gained."""
    ws, start = conflicted
    git(ws, "merge", "--abort")

    assert _finish_base_merge(ws, "work", "main", start, _A_CONFLICT, MagicMock()) == []
    with pytest.raises(BranchIntegrityError, match="b.txt"):
        _assert_branch_integrity(ws, "main", start)
