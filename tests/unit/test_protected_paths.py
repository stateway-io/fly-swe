"""Harness enforcement of repos[].project.protected_paths (runner/protected_paths.py)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from issue_agent.runner.executor import _run_git
from issue_agent.runner.protected_paths import enforce, is_protected, normalize, reverted_note

if TYPE_CHECKING:
    from pathlib import Path


class TestNormalize:
    def test_strips_prefixes_and_whitespace(self) -> None:
        assert normalize([" ./migrations/ ", "/src/engine", "././a.txt"]) == [
            "migrations/",
            "src/engine",
            "a.txt",
        ]

    def test_drops_empty_and_duplicates(self) -> None:
        assert normalize(["", "  ", "/", "a/", "./a/"]) == ["a/"]


class TestIsProtected:
    @pytest.mark.parametrize("pattern", ["migrations/", "migrations"])
    def test_directory_prefix(self, pattern: str) -> None:
        assert is_protected("migrations/0001.sql", [pattern])
        assert is_protected("migrations/nested/0002.sql", [pattern])

    def test_no_false_prefix_match(self) -> None:
        assert not is_protected("migrations_old/0001.sql", ["migrations/"])
        assert not is_protected("src/migrations/0001.sql", ["migrations/"])

    def test_exact_file(self) -> None:
        assert is_protected("src/engine/schema.py", ["src/engine/schema.py"])
        assert not is_protected("src/engine/schema.pyc", ["src/engine/schema.py"])

    def test_glob(self) -> None:
        assert is_protected("db/0001_init.sql", ["*.sql"])
        assert is_protected(".github/workflows/ci.yml", [".github/workflows/*.yml"])
        assert not is_protected("db/0001_init.py", ["*.sql"])

    def test_no_patterns(self) -> None:
        assert not is_protected("anything", [])


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "migrations").mkdir()
    (path / "migrations" / "0001.sql").write_text("create table a;\n")
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("print('v1')\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    return path, _git(path, "rev-parse", "HEAD").strip()


def _staged(repo: Path, sha: str) -> set[str]:
    out = _git(repo, "diff", "--cached", "--name-only", "--no-renames", sha)
    return set(out.split())


class TestEnforce:
    def test_no_patterns_makes_no_git_call(self, tmp_path: Path) -> None:
        run_git = MagicMock()
        assert enforce(tmp_path, ["", " "], "abc", run_git) == []
        run_git.assert_not_called()

    def test_reverts_modified_protected_keeps_others(self, repo: tuple[Path, str]) -> None:
        path, sha = repo
        (path / "migrations" / "0001.sql").write_text("drop table a;\n")
        (path / "src" / "app.py").write_text("print('v2')\n")
        log = MagicMock()

        reverted = enforce(path, ["migrations/"], sha, _run_git, log)

        assert reverted == ["migrations/0001.sql"]
        assert (path / "migrations" / "0001.sql").read_text() == "create table a;\n"
        assert (path / "src" / "app.py").read_text() == "print('v2')\n"
        assert _staged(path, sha) == {"src/app.py"}
        log.warning.assert_called_once_with(
            "protected_paths_reverted", paths=["migrations/0001.sql"]
        )

    def test_removes_added_protected_file(self, repo: tuple[Path, str]) -> None:
        path, sha = repo
        (path / "migrations" / "0002.sql").write_text("new\n")

        assert enforce(path, ["migrations"], sha, _run_git) == ["migrations/0002.sql"]
        assert not (path / "migrations" / "0002.sql").exists()
        assert _git(path, "status", "--porcelain") == ""

    def test_restores_deleted_protected_file(self, repo: tuple[Path, str]) -> None:
        path, sha = repo
        (path / "migrations" / "0001.sql").unlink()

        assert enforce(path, ["migrations/"], sha, _run_git) == ["migrations/0001.sql"]
        assert (path / "migrations" / "0001.sql").read_text() == "create table a;\n"
        assert _git(path, "status", "--porcelain") == ""

    def test_rename_out_of_protected_dir(self, repo: tuple[Path, str]) -> None:
        path, sha = repo
        _git(path, "mv", "migrations/0001.sql", "src/0001.sql")

        assert enforce(path, ["migrations/"], sha, _run_git) == ["migrations/0001.sql"]
        assert (path / "migrations" / "0001.sql").exists()
        # The new copy outside the protected dir is not protected: it stays.
        assert _staged(path, sha) == {"src/0001.sql"}

    def test_rename_into_protected_dir(self, repo: tuple[Path, str]) -> None:
        path, sha = repo
        _git(path, "mv", "src/app.py", "migrations/app.py")

        assert enforce(path, ["migrations/"], sha, _run_git) == ["migrations/app.py"]
        assert not (path / "migrations" / "app.py").exists()
        # Only the deletion of the unprotected source is left.
        assert _staged(path, sha) == {"src/app.py"}

    def test_reverts_agent_commits_since_source(self, repo: tuple[Path, str]) -> None:
        path, sha = repo
        (path / "migrations" / "0001.sql").write_text("changed\n")
        _git(path, "commit", "-qam", "agent commit")
        _git(path, "reset", "--soft", sha)

        assert enforce(path, ["migrations/"], sha, _run_git) == ["migrations/0001.sql"]
        assert _git(path, "status", "--porcelain") == ""

    def test_glob_char_in_filename_is_literal(self, repo: tuple[Path, str]) -> None:
        path, sha = repo
        (path / "migrations" / "a*.sql").write_text("x\n")
        (path / "migrations" / "ab.sql").write_text("keep\n")

        reverted = enforce(path, ["migrations/a*.sql"], sha, _run_git)

        # `a*.sql` (glob) matches both; both are new, so both are removed —
        # but the restore itself treats each name literally.
        assert reverted == ["migrations/a*.sql", "migrations/ab.sql"]
        assert _git(path, "status", "--porcelain") == ""

    def test_nothing_protected_changed(self, repo: tuple[Path, str]) -> None:
        path, sha = repo
        (path / "src" / "app.py").write_text("print('v2')\n")
        log = MagicMock()

        assert enforce(path, ["migrations/"], sha, _run_git, log) == []
        log.warning.assert_not_called()
        assert _staged(path, sha) == {"src/app.py"}


class TestRevertedNote:
    def test_empty(self) -> None:
        assert reverted_note([]) == ""

    def test_lists_paths(self) -> None:
        note = reverted_note(["a.sql", "b/c.py"])
        assert "`a.sql`, `b/c.py`" in note
        assert "protected paths" in note
