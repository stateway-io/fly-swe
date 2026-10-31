from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import filelock

from issue_agent.logging import get_logger
from issue_agent.runner.toolchains.exceptions import ToolchainError, ToolchainLockError
from issue_agent.runner.toolchains.registry import BUILTIN_PLUGINS

if TYPE_CHECKING:
    from issue_agent.runner.toolchains.base import ToolchainPlugin
    from issue_agent.runner.toolchains.spec import ToolchainSpec

logger = get_logger(__name__)


class ToolchainManager:
    """Detect, install and cache toolchains for agent workspaces.

    Toolchains live under ``tools_root/<name>/<version>`` on the persistent
    Fly volume. Installations are atomic and guarded by file locks so
    concurrent tasks don't race.
    """

    def __init__(
        self,
        tools_root: Path,
        *,
        plugins: dict[str, type[ToolchainPlugin]] | None = None,
        default_node_version: str = "20",
        nodejs_mirror: str = "https://nodejs.org/dist",
        install_timeout: float = 300.0,
        lock_timeout: float = 600.0,
    ) -> None:
        self.tools_root = Path(tools_root)
        self.plugins: dict[str, ToolchainPlugin] = {}
        for name, plugin_cls in (plugins or BUILTIN_PLUGINS).items():
            if name == "nodejs":
                self.plugins[name] = plugin_cls(
                    self.tools_root,
                    default_version=default_node_version,
                    mirror=nodejs_mirror,
                )
            else:
                self.plugins[name] = plugin_cls(self.tools_root)
        self.install_timeout = install_timeout
        self.lock_timeout = lock_timeout

    def detect(self, workspace_path: Path) -> list[ToolchainSpec]:
        """Return the list of toolchains needed for ``workspace_path``."""
        specs: list[ToolchainSpec] = []
        for plugin in self.plugins.values():
            try:
                spec = plugin.detect(workspace_path)
            except Exception as exc:
                logger.warning(
                    "toolchain_detect_failed",
                    plugin=plugin.name,
                    workspace=str(workspace_path),
                    error=str(exc),
                )
                continue
            if spec is not None:
                specs.append(spec)
        return specs

    def ensure(self, specs: list[ToolchainSpec]) -> dict[str, str]:
        """Install missing toolchains and return a combined env dict."""
        env: dict[str, str] = {}
        for spec in specs:
            self._ensure_one(spec)
            plugin = self._plugin_for(spec.name)
            spec_env = plugin.env(spec)
            env = _merge_env(env, spec_env)
        return env

    def _ensure_one(self, spec: ToolchainSpec) -> None:
        if spec.install_dir.exists() and any(spec.install_dir.iterdir()):
            logger.info("toolchain_already_installed", name=spec.name, version=spec.version)
            return

        lock_path = self.tools_root / f".{spec.name}-{spec.version}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = filelock.FileLock(str(lock_path), timeout=self.lock_timeout)

        try:
            with lock:
                # Double-check after acquiring the lock
                if spec.install_dir.exists() and any(spec.install_dir.iterdir()):
                    logger.info(
                        "toolchain_installed_by_peer",
                        name=spec.name,
                        version=spec.version,
                    )
                    return

                plugin = self._plugin_for(spec.name)
                logger.info(
                    "installing_toolchain",
                    name=spec.name,
                    version=spec.version,
                    url=spec.source_url,
                )
                plugin.install(spec)
        except filelock.Timeout as exc:
            raise ToolchainLockError(
                f"timed out waiting for {spec.name} {spec.version} install lock"
            ) from exc
        except Exception as exc:
            if isinstance(exc, ToolchainError):
                raise
            raise ToolchainError(f"failed to ensure {spec.name} {spec.version}: {exc}") from exc

    def _plugin_for(self, name: str) -> ToolchainPlugin:
        try:
            return self.plugins[name]
        except KeyError as exc:
            raise ToolchainError(f"unknown toolchain plugin: {name}") from exc


def _merge_env(base: dict[str, str], extra: dict[str, str]) -> dict[str, str]:
    """Merge env dicts, prepending PATH entries instead of overwriting."""
    result = dict(base)
    for key, value in extra.items():
        if key == "PATH" and key in result:
            result[key] = f"{value}{os.pathsep}{result[key]}"
        else:
            result[key] = value
    return result


def env_with_system_path(toolchain_env: dict[str, str]) -> dict[str, str]:
    """Return ``toolchain_env`` merged with the current worker's PATH.

    Useful when the harness needs to run commands (e.g. ``npm ci``) using a
    discovered toolchain but also needs system binaries like ``git``.
    The toolchain PATH is kept first so its binaries win over system defaults.
    """
    merged = dict(toolchain_env)
    system_path = os.environ.get("PATH", "")
    toolchain_path = merged.get("PATH", "")
    if toolchain_path and system_path:
        merged["PATH"] = f"{toolchain_path}{os.pathsep}{system_path}"
    elif system_path:
        merged["PATH"] = system_path
    return merged
