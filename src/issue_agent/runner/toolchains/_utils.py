from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from issue_agent.logging import get_logger
from issue_agent.runner.toolchains.exceptions import ToolchainInstallError

if TYPE_CHECKING:
    from issue_agent.runner.toolchains.spec import ToolchainSpec

logger = get_logger(__name__)


class ToolchainDownloadError(ToolchainInstallError):
    pass


class ToolchainChecksumError(ToolchainInstallError):
    pass


class ToolchainExtractError(ToolchainInstallError):
    pass


def _sibling(path: Path, suffix: str) -> Path:
    """Return a sibling of ``path`` with ``suffix`` appended to its full name.

    ``Path.with_suffix`` treats the trailing ``.0``/``.5`` of a version
    directory (e.g. ``20.17.0``) as a file extension and replaces it, which
    makes two different patch versions under the same major.minor (e.g.
    ``20.17.0`` and ``20.17.5``) collide on the same ``.tmp``/``.old``/
    ``.extract`` sibling path. Appending instead of replacing keeps every
    version's scratch directories distinct.
    """
    return path.with_name(path.name + suffix)


def _atomic_dir_swap(target: Path, temp_dir: Path) -> None:
    """Move ``temp_dir`` to ``target`` atomically.

    If ``target`` already exists, replace it. Parent directories are created
    as needed. This assumes the caller holds the install lock.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    old_target = _sibling(target, ".old")
    try:
        if target.exists():
            target.rename(old_target)
        temp_dir.rename(target)
    except Exception:
        # Best-effort rollback
        if not target.exists() and old_target.exists():
            old_target.rename(target)
        raise
    finally:
        if old_target.exists():
            shutil.rmtree(old_target, ignore_errors=True)


def download_file(url: str, dest: Path, timeout: float = 120.0) -> None:
    """Download ``url`` to ``dest`` with streaming and a timeout."""
    logger.info("downloading_toolchain", url=url, dest=str(dest))
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"}:
        raise ToolchainDownloadError(f"refusing to download from non-HTTP scheme: {url}")

    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=timeout) as response:
            response.raise_for_status()
            with dest.open("wb") as f:
                for chunk in response.iter_bytes(chunk_size=65536):
                    f.write(chunk)
    except Exception as exc:
        raise ToolchainDownloadError(f"failed to download {url}: {exc}") from exc


def verify_checksum(file_path: Path, expected: str, algorithm: str = "sha256") -> None:
    """Verify ``file_path`` against an expected checksum."""
    if algorithm != "sha256":
        raise ToolchainChecksumError(f"unsupported checksum algorithm: {algorithm}")

    h = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual.lower() != expected.lower():
        raise ToolchainChecksumError(
            f"checksum mismatch for {file_path}: expected {expected}, got {actual}"
        )


def extract_archive(archive_path: Path, dest_dir: Path, strip_components: int = 1) -> None:
    """Extract ``archive_path`` into ``dest_dir``, stripping ``strip_components`` leading path components.

    Supports ``.tar.xz``, ``.tar.gz``, ``.tgz`` and ``.zip``.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = archive_path.suffix.lower()
    name = archive_path.name.lower()

    try:
        if name.endswith(".tar.xz") or name.endswith(".tar.gz") or name.endswith(".tgz"):
            _extract_tar(archive_path, dest_dir, strip_components)
        elif suffix == ".zip":
            _extract_zip(archive_path, dest_dir, strip_components)
        else:
            raise ToolchainExtractError(f"unsupported archive format: {archive_path}")
    except Exception as exc:
        raise ToolchainExtractError(f"failed to extract {archive_path}: {exc}") from exc


def _extract_tar(archive_path: Path, dest_dir: Path, strip_components: int) -> None:
    mode = "r:xz" if archive_path.name.lower().endswith(".tar.xz") else "r:gz"
    temp_extract = _sibling(dest_dir, ".extract")
    shutil.rmtree(temp_extract, ignore_errors=True)
    temp_extract.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive_path, mode) as tf:
        tf.extractall(path=temp_extract)

    _move_stripped(temp_extract, dest_dir, strip_components)
    shutil.rmtree(temp_extract, ignore_errors=True)


def _extract_zip(archive_path: Path, dest_dir: Path, strip_components: int) -> None:
    temp_extract = _sibling(dest_dir, ".extract")
    shutil.rmtree(temp_extract, ignore_errors=True)
    temp_extract.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive_path, "r") as zf:
        zf.extractall(path=temp_extract)

    _move_stripped(temp_extract, dest_dir, strip_components)
    shutil.rmtree(temp_extract, ignore_errors=True)


def _move_stripped(source: Path, dest: Path, strip_components: int) -> None:
    """Move the contents of ``source`` to ``dest`` stripping leading directories."""
    current = source
    for _ in range(strip_components):
        entries = [e for e in current.iterdir() if e.name not in {".DS_Store"}]
        if len(entries) != 1 or not entries[0].is_dir():
            raise ToolchainExtractError(
                f"cannot strip {strip_components} components from {source}"
            )
        current = entries[0]

    dest.mkdir(parents=True, exist_ok=True)
    for item in current.iterdir():
        target = dest / item.name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True) if item.is_dir() else target.unlink(missing_ok=True)
        shutil.move(str(item), str(target))


def make_executable(file_path: Path) -> None:
    """Add executable permission to ``file_path``."""
    if file_path.exists():
        os.chmod(file_path, os.stat(file_path).st_mode | 0o111)


def download_and_install(spec: ToolchainSpec, strip_components: int = 1) -> None:
    """Helper for plugins: download, verify checksum and extract a toolchain archive."""
    if spec.bin_dir.exists() and any(spec.bin_dir.iterdir()):
        logger.info("toolchain_already_present", name=spec.name, version=spec.version)
        return

    with tempfile.TemporaryDirectory(prefix=f"{spec.name}-{spec.version}-") as tmp:
        tmp_path = Path(tmp)
        archive_name = Path(urlparse(spec.source_url).path).name or "archive.tar.xz"
        archive_path = tmp_path / archive_name

        download_file(spec.source_url, archive_path)

        if spec.checksum:
            verify_checksum(archive_path, spec.checksum)
        else:
            logger.warning("no_checksum_provided", name=spec.name, version=spec.version)

        temp_install = _sibling(spec.install_dir, ".tmp")
        shutil.rmtree(temp_install, ignore_errors=True)
        extract_archive(archive_path, temp_install, strip_components)

        # Ensure binaries are executable (tarball extraction usually preserves this,
        # but zip extraction on Linux may not).
        if temp_install.exists():
            for bin_file in (temp_install / "bin").glob("*"):
                make_executable(bin_file)

        _atomic_dir_swap(spec.install_dir, temp_install)

    logger.info("toolchain_installed", name=spec.name, version=spec.version, path=str(spec.install_dir))


def run_command(
    command: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command and return the result."""
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )
