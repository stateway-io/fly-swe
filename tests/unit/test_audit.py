from __future__ import annotations

import gzip
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from issue_agent.observability.audit import (
    _audit_path,
    _now,
    _symlink_path,
    _today,
    prune_old_audit_files,
    read_audit_entries,
    write_audit_entry,
)


def _make_old_file(data_dir: str, days_ago: int) -> Path:
    date = (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    path = _audit_path(data_dir, date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"event": "old"}) + "\n")
    return path


class TestWriteAuditEntry:
    def test_creates_file(self, tmp_path: Path):
        write_audit_entry(str(tmp_path), {"event": "test"})
        today = _today()
        expected = tmp_path / f"audit.{today}.log"
        assert expected.exists()

    def test_contains_timestamp_and_level(self, tmp_path: Path):
        write_audit_entry(str(tmp_path), {"event": "test"})
        today = _today()
        path = tmp_path / f"audit.{today}.log"
        data = json.loads(path.read_text())
        assert "_timestamp" in data
        assert data["level"] == "info"

    def test_custom_level(self, tmp_path: Path):
        write_audit_entry(str(tmp_path), {"event": "err"}, level="error")
        today = _today()
        path = tmp_path / f"audit.{today}.log"
        data = json.loads(path.read_text())
        assert data["level"] == "error"

    def test_appends_multiple_entries(self, tmp_path: Path):
        write_audit_entry(str(tmp_path), {"event": "first"})
        write_audit_entry(str(tmp_path), {"event": "second"})
        today = _today()
        path = tmp_path / f"audit.{today}.log"
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["event"] == "first"
        assert json.loads(lines[1])["event"] == "second"

    def test_creates_parent_dir(self, tmp_path: Path):
        nested = str(tmp_path / "sub" / "dir")
        write_audit_entry(nested, {"event": "nested"})
        today = _today()
        expected = Path(nested) / f"audit.{today}.log"
        assert expected.exists()

    def test_non_string_default_serialized(self, tmp_path: Path):
        write_audit_entry(str(tmp_path), {"num": 42, "flag": True})
        today = _today()
        path = tmp_path / f"audit.{today}.log"
        data = json.loads(path.read_text())
        assert data["num"] == 42
        assert data["flag"] is True

    def test_symlink_created(self, tmp_path: Path):
        write_audit_entry(str(tmp_path), {"event": "link_test"})
        link = _symlink_path(str(tmp_path))
        assert link.is_symlink()
        today = _today()
        expected = tmp_path / f"audit.{today}.log"
        assert link.resolve() == expected.resolve()

    def test_symlink_updated_on_new_day(self, tmp_path: Path):
        write_audit_entry(str(tmp_path), {"event": "day1"})
        yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        old_path = _audit_path(str(tmp_path), yesterday)
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_text(json.dumps({"event": "old"}) + "\n")
        os.unlink(_symlink_path(str(tmp_path)))
        os.symlink(old_path.name, _symlink_path(str(tmp_path)))
        write_audit_entry(str(tmp_path), {"event": "day2"})
        today = _today()
        expected = tmp_path / f"audit.{today}.log"
        assert _symlink_path(str(tmp_path)).resolve() == expected.resolve()


class TestReadAuditEntries:
    def test_read_returns_entries(self, tmp_path: Path):
        write_audit_entry(str(tmp_path), {"event": "one"})
        write_audit_entry(str(tmp_path), {"event": "two"})
        entries = read_audit_entries(str(tmp_path))
        assert len(entries) == 2
        assert entries[0]["event"] == "one"
        assert entries[1]["event"] == "two"

    def test_read_specific_date(self, tmp_path: Path):
        old_date = "2024-01-01"
        path = _audit_path(str(tmp_path), old_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"event": "ancient"}) + "\n")
        entries = read_audit_entries(str(tmp_path), date=old_date)
        assert len(entries) == 1
        assert entries[0]["event"] == "ancient"

    def test_read_nonexistent_date_returns_empty(self, tmp_path: Path):
        entries = read_audit_entries(str(tmp_path), date="2099-12-31")
        assert entries == []

    def test_read_respects_max_entries(self, tmp_path: Path):
        for i in range(10):
            write_audit_entry(str(tmp_path), {"event": f"e{i}"})
        entries = read_audit_entries(str(tmp_path), max_entries=3)
        assert len(entries) == 3

    def test_read_skips_corrupted_lines(self, tmp_path: Path):
        path = _audit_path(str(tmp_path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"event": "good"})
            + "\ncorrupted_line\n"
            + json.dumps({"event": "good2"})
            + "\n"
        )
        entries = read_audit_entries(str(tmp_path))
        assert len(entries) == 2


class TestPruneOldAuditFiles:
    def test_removes_files_older_than_max_days(self, tmp_path: Path):
        old = _make_old_file(str(tmp_path), days_ago=100)
        assert old.exists()
        result = prune_old_audit_files(str(tmp_path), max_days=90, compress=False)
        assert not old.exists()
        assert old in result

    def test_keeps_files_within_max_days(self, tmp_path: Path):
        recent = _make_old_file(str(tmp_path), days_ago=10)
        result = prune_old_audit_files(str(tmp_path), max_days=90, compress=False)
        assert recent.exists()
        assert recent not in result

    def test_compress_creates_gz_and_removes_original(self, tmp_path: Path):
        old = _make_old_file(str(tmp_path), days_ago=100)
        result = prune_old_audit_files(str(tmp_path), max_days=90, compress=True)
        gz_path = old.with_suffix(old.suffix + ".gz")
        assert gz_path.exists()
        assert not old.exists()
        assert gz_path in result
        with gzip.open(gz_path, "rt") as f:
            content = f.read()
            assert "old" in content

    def test_ignores_non_audit_files(self, tmp_path: Path):
        other = tmp_path / "other.log"
        other.write_text("data\n")
        old = _make_old_file(str(tmp_path), days_ago=100)
        prune_old_audit_files(str(tmp_path), max_days=90, compress=False)
        assert other.exists()
        assert not old.exists()

    def test_no_files_returns_empty(self, tmp_path: Path):
        result = prune_old_audit_files(str(tmp_path), max_days=90)
        assert result == []

    def test_nonexistent_dir_returns_empty(self):
        result = prune_old_audit_files("/nonexistent/path", max_days=90)
        assert result == []


class TestUtilityFunctions:
    def test_today_format(self):
        today = _today()
        assert len(today) == 10
        datetime.strptime(today, "%Y-%m-%d")

    def test_now_format(self):
        now = _now()
        assert now.endswith("Z")
        assert len(now) == 24

    def test_audit_path_default(self, tmp_path: Path):
        today = _today()
        path = _audit_path(str(tmp_path))
        assert path == tmp_path / f"audit.{today}.log"

    def test_audit_path_custom_date(self, tmp_path: Path):
        path = _audit_path(str(tmp_path), date="2025-06-15")
        assert path == tmp_path / "audit.2025-06-15.log"

    def test_symlink_path(self, tmp_path: Path):
        path = _symlink_path(str(tmp_path))
        assert path == tmp_path / "audit.log"
