from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class ToolchainSpec:
    """Specification for a toolchain installation.

    Attributes:
        name: Plugin identifier, e.g. "nodejs".
        version: Exact version string, e.g. "20.17.0".
        install_dir: Directory where the toolchain should be installed.
        source_url: URL to download the toolchain archive.
        checksum: Optional SHA-256 checksum for verification.
    """

    name: str
    version: str
    install_dir: Path
    source_url: str
    checksum: str | None = None

    @property
    def bin_dir(self) -> Path:
        return self.install_dir / "bin"
