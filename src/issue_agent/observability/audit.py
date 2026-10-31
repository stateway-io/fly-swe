from __future__ import annotations

import gzip
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_AUDIT_PREFIX = "audit."
_AUDIT_SUFFIX = ".log"
_AUDIT_SYMLINK = "audit.log"


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _now() -> str:
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond:06d}"[:3] + "Z"


def _audit_path(data_dir: str, date: str | None = None) -> Path:
    return Path(data_dir) / f"{_AUDIT_PREFIX}{(date or _today())}{_AUDIT_SUFFIX}"


def _symlink_path(data_dir: str) -> Path:
    return Path(data_dir) / _AUDIT_SYMLINK


def _update_symlink(data_dir: str, target: Path) -> None:
    link = _symlink_path(data_dir)
    try:
        if link.is_symlink() and link.resolve() == target.resolve():
            return
        link.unlink(missing_ok=True)
        link.symlink_to(target.name)
    except OSError:
        pass


def write_audit_entry(
    data_dir: str,
    entry: dict[str, Any],
    level: str = "info",
) -> None:
    entry["_timestamp"] = _now()
    entry["level"] = level
    path = _audit_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    _update_symlink(data_dir, path)


def read_audit_entries(
    data_dir: str,
    date: str | None = None,
    max_entries: int = 100,
) -> list[dict[str, Any]]:
    path = _audit_path(data_dir, date)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(entries) >= max_entries:
                break
    return entries


def prune_old_audit_files(
    data_dir: str,
    max_days: int = 90,
    compress: bool = True,
) -> list[Path]:
    cutoff = datetime.now(UTC) - timedelta(days=max_days)
    removed: list[Path] = []
    data_path = Path(data_dir)
    if not data_path.is_dir():
        return removed

    for child in sorted(data_path.iterdir()):
        if not child.name.startswith(_AUDIT_PREFIX):
            continue
        if not child.name.endswith(_AUDIT_SUFFIX):
            continue
        date_str = child.name[len(_AUDIT_PREFIX) : -len(_AUDIT_SUFFIX)]
        try:
            file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            continue
        if file_date >= cutoff:
            continue
        if compress:
            gz_path = child.with_suffix(child.suffix + ".gz")
            if gz_path.exists():
                child.unlink()
            else:
                with child.open("rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                child.unlink()
            removed.append(gz_path)
        else:
            child.unlink()
            removed.append(child)

    return removed
