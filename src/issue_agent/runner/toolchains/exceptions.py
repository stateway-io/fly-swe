from __future__ import annotations


class ToolchainError(Exception):
    """Base exception for toolchain management."""


class ToolchainDetectError(ToolchainError):
    """Failed to detect which toolchain/version is required."""


class ToolchainInstallError(ToolchainError):
    """Failed to download or install a toolchain."""


class ToolchainLockError(ToolchainError):
    """Could not acquire the install lock in time."""
