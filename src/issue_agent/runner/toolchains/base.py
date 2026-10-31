from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from pathlib import Path

    from issue_agent.runner.toolchains.spec import ToolchainSpec


class ToolchainPlugin(ABC):
    """Base class for toolchain plugins.

    Each supported stack (Node.js, Go, Rust, ...) implements this interface
    and registers itself in ``issue_agent.runner.toolchains.registry``.
    """

    name: ClassVar[str]

    @abstractmethod
    def detect(self, workspace_path: Path) -> ToolchainSpec | None:
        """Detect whether this toolchain is needed for ``workspace_path``.

        Returns a fully populated ``ToolchainSpec`` (including resolved
        version and download URL) when the plugin applies, or ``None``.
        """

    @abstractmethod
    def install(self, spec: ToolchainSpec) -> None:
        """Download and install ``spec`` into ``spec.install_dir``.

        Implementations must be idempotent where possible and should raise
        ``ToolchainInstallError`` on failure.
        """

    @abstractmethod
    def env(self, spec: ToolchainSpec) -> dict[str, str]:
        """Return environment variables to expose the toolchain.

        Typically this is just an extended ``PATH``, but plugins may add
        stack-specific variables (e.g. ``NODE_PATH``).
        """
