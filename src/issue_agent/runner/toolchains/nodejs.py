from __future__ import annotations

import json
import re
from pathlib import Path
from typing import ClassVar

import httpx

from issue_agent.logging import get_logger
from issue_agent.runner.toolchains._utils import download_and_install
from issue_agent.runner.toolchains.base import ToolchainPlugin
from issue_agent.runner.toolchains.exceptions import ToolchainDetectError, ToolchainInstallError
from issue_agent.runner.toolchains.spec import ToolchainSpec

logger = get_logger(__name__)

NODEJS_PLATFORM = "linux-x64"
NODEJS_ARCHIVE_EXT = "tar.xz"
NODEJS_DEFAULT_MIRROR = "https://nodejs.org/dist"

# Version specifiers like "20", ">=18.0.0 <21", "^20.10.0", "lts/*"
_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")


class NodejsPlugin(ToolchainPlugin):
    """Node.js toolchain plugin.

    Detects Node.js projects via ``package.json`` and resolves the requested
    version from ``.nvmrc``, ``package.json → engines.node`` or a configured
    default (falling back to the latest LTS).
    """

    name: ClassVar[str] = "nodejs"

    def __init__(
        self, tools_root: Path, default_version: str = "20", mirror: str = NODEJS_DEFAULT_MIRROR
    ) -> None:
        self.tools_root = tools_root
        self.default_version = default_version
        self.mirror = mirror.rstrip("/")
        self._index_cache: list[dict] | None = None

    def detect(self, workspace_path: Path) -> ToolchainSpec | None:
        package_json = workspace_path / "package.json"
        if not package_json.is_file():
            return None

        requested = self._resolve_requested_version(workspace_path)
        exact_version = self._resolve_exact_version(requested)
        install_dir = self.tools_root / self.name / exact_version
        source_url = (
            f"{self.mirror}/v{exact_version}/"
            f"node-v{exact_version}-{NODEJS_PLATFORM}.{NODEJS_ARCHIVE_EXT}"
        )
        checksum = self._fetch_checksum(exact_version, source_url)

        return ToolchainSpec(
            name=self.name,
            version=exact_version,
            install_dir=install_dir,
            source_url=source_url,
            checksum=checksum,
        )

    def install(self, spec: ToolchainSpec) -> None:
        try:
            download_and_install(spec, strip_components=1)
        except Exception as exc:
            raise ToolchainInstallError(f"failed to install Node.js {spec.version}: {exc}") from exc

    def env(self, spec: ToolchainSpec) -> dict[str, str]:
        bin_dir = spec.bin_dir
        lib_dir = spec.install_dir / "lib" / "node_modules"
        return {
            "PATH": f"{bin_dir}{_path_separator()}{_current_path()}",
            "NODE_PATH": str(lib_dir),
        }

    # ------------------------------------------------------------------
    # Version resolution
    # ------------------------------------------------------------------

    def _resolve_requested_version(self, workspace_path: Path) -> str:
        """Pick a requested Node version from project files or config default."""
        nvmrc = workspace_path / ".nvmrc"
        if nvmrc.is_file():
            version = nvmrc.read_text(encoding="utf-8").strip()
            if version:
                logger.info("nodejs_version_from_nvmrc", version=version)
                return version

        package_json = workspace_path / "package.json"
        if package_json.is_file():
            try:
                data = json.loads(package_json.read_text(encoding="utf-8"))
                engines = data.get("engines", {})
                version = engines.get("node") or data.get("volta", {}).get("node")
                if version:
                    logger.info("nodejs_version_from_package_json", version=version)
                    return version
            except (json.JSONDecodeError, OSError):
                pass

        logger.info("nodejs_version_using_default", version=self.default_version)
        return self.default_version

    def _resolve_exact_version(self, requested: str) -> str:
        """Convert a requested version specifier into an exact semver string."""
        requested = requested.strip()

        # Already exact, e.g. "20.17.0"
        match = _VERSION_RE.fullmatch(requested.lstrip("v").lstrip("="))
        if match:
            major = match.group(1)
            minor = match.group(2)
            patch = match.group(3)
            if minor and patch:
                return f"{major}.{minor}.{patch}"
            return self._latest_matching(major=major, lts_only=False)

        # "lts/*" or aliases
        if requested.lower() in {"lts/*", "lts", "lts-latest"}:
            return self._latest_matching(lts_only=True)

        # Range-ish specifiers: take the major from the first numeric segment
        match = _VERSION_RE.search(requested)
        if match:
            major = match.group(1)
            return self._latest_matching(major=major, lts_only=False)

        raise ToolchainDetectError(f"could not resolve Node.js version from '{requested}'")

    def _latest_matching(self, major: str | None = None, lts_only: bool = False) -> str:
        index = self._fetch_index()
        for release in index:
            version = release.get("version", "").lstrip("v")
            if not version:
                continue
            if major and not version.startswith(f"{major}."):
                continue
            if lts_only and release.get("lts") is False:
                continue
            return version

        available = ", ".join(r["version"].lstrip("v") for r in index[:5])
        raise ToolchainDetectError(
            f"no Node.js release matched major={major} lts={lts_only}; recent releases: {available}"
        )

    def _fetch_index(self) -> list[dict]:
        if self._index_cache is not None:
            return self._index_cache

        index_url = f"{self.mirror}/index.json"
        try:
            response = httpx.get(index_url, follow_redirects=True, timeout=30.0)
            response.raise_for_status()
            index = response.json()
        except Exception as exc:
            raise ToolchainDetectError(f"failed to fetch Node.js release index: {exc}") from exc

        # index.json is already ordered newest-first by nodejs.org
        self._index_cache = index
        return index

    def _fetch_checksum(self, version: str, source_url: str) -> str | None:
        """Fetch SHA256 for the tarball from the mirror's SHASUMS256.txt."""
        shasums_url = f"{self.mirror}/v{version}/SHASUMS256.txt"
        archive_name = Path(source_url).name
        try:
            response = httpx.get(shasums_url, follow_redirects=True, timeout=30.0)
            response.raise_for_status()
            for line in response.text.splitlines():
                parts = line.strip().split()
                if len(parts) == 2 and parts[1] == archive_name:
                    return parts[0]
        except Exception:
            logger.warning("nodejs_checksum_lookup_failed", version=version, url=shasums_url)
        return None


def _current_path() -> str:
    return __import__("os").environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")


def _path_separator() -> str:
    return __import__("os").pathsep
