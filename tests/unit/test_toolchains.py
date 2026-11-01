from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from issue_agent.runner.toolchains import (
    NodejsPlugin,
    ToolchainManager,
    ToolchainSpec,
    env_with_system_path,
)
from issue_agent.runner.toolchains.exceptions import ToolchainDetectError

if TYPE_CHECKING:
    from pathlib import Path


class TestNodejsPluginDetect:
    def test_no_package_json_returns_none(self, tmp_path: Path) -> None:
        plugin = NodejsPlugin(tmp_path / "tools")
        assert plugin.detect(tmp_path) is None

    def test_package_json_without_engines_uses_default_version(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"name": "x"}))
        plugin = NodejsPlugin(tmp_path / "tools", default_version="20")

        with patch.object(plugin, "_latest_matching", return_value="20.17.0"):
            spec = plugin.detect(tmp_path)

        assert spec is not None
        assert spec.name == "nodejs"
        assert spec.version == "20.17.0"
        assert "node-v20.17.0-linux-x64.tar.xz" in spec.source_url

    def test_nvmrc_takes_priority(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"name": "x"}))
        (tmp_path / ".nvmrc").write_text("18.20.4\n")
        plugin = NodejsPlugin(tmp_path / "tools", default_version="20")

        spec = plugin.detect(tmp_path)

        assert spec is not None
        assert spec.version == "18.20.4"

    def test_engines_node_respected(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "x", "engines": {"node": ">=20.0.0 <21.0.0"}})
        )
        plugin = NodejsPlugin(tmp_path / "tools", default_version="18")

        with patch.object(plugin, "_latest_matching", return_value="20.17.0") as mock:
            spec = plugin.detect(tmp_path)

        assert spec is not None
        assert spec.version == "20.17.0"
        mock.assert_called_once_with(major="20", lts_only=False)

    def test_volta_node_respected(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "x", "volta": {"node": "20.17.0"}})
        )
        plugin = NodejsPlugin(tmp_path / "tools", default_version="18")

        spec = plugin.detect(tmp_path)

        assert spec is not None
        assert spec.version == "20.17.0"


class TestNodejsMirror:
    """A configured mirror must actually be used for every nodejs.org URL —
    not just stored and ignored (the plugin used to hardcode nodejs.org)."""

    _MIRROR = "https://internal-mirror.example/dist"

    def test_source_url_uses_configured_mirror(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"name": "x"}))
        plugin = NodejsPlugin(tmp_path / "tools", default_version="20.17.0", mirror=self._MIRROR)

        with patch.object(plugin, "_fetch_checksum", return_value=None):
            spec = plugin.detect(tmp_path)

        assert spec is not None
        assert spec.source_url.startswith(self._MIRROR)
        assert "nodejs.org" not in spec.source_url

    def test_fetch_index_uses_configured_mirror(self, tmp_path: Path) -> None:
        plugin = NodejsPlugin(tmp_path / "tools", mirror=self._MIRROR)

        with patch("issue_agent.runner.toolchains.nodejs.httpx.get") as mock_get:
            mock_get.return_value.json.return_value = [{"version": "v20.17.0", "lts": "Iron"}]
            mock_get.return_value.raise_for_status.return_value = None
            plugin._fetch_index()

        called_url = mock_get.call_args[0][0]
        assert called_url == f"{self._MIRROR}/index.json"

    def test_fetch_checksum_uses_configured_mirror(self, tmp_path: Path) -> None:
        plugin = NodejsPlugin(tmp_path / "tools", mirror=self._MIRROR)

        with patch("issue_agent.runner.toolchains.nodejs.httpx.get") as mock_get:
            mock_get.return_value.text = "abc123  node-v20.17.0-linux-x64.tar.xz\n"
            mock_get.return_value.raise_for_status.return_value = None
            checksum = plugin._fetch_checksum(
                "20.17.0", f"{self._MIRROR}/v20.17.0/node-v20.17.0-linux-x64.tar.xz"
            )

        called_url = mock_get.call_args[0][0]
        assert called_url == f"{self._MIRROR}/v20.17.0/SHASUMS256.txt"
        assert checksum == "abc123"


class TestNodejsVersionResolution:
    def test_exact_version_passthrough(self, tmp_path: Path) -> None:
        plugin = NodejsPlugin(tmp_path / "tools")
        assert plugin._resolve_exact_version("20.17.0") == "20.17.0"

    def test_major_version_resolves_to_latest(self, tmp_path: Path) -> None:
        plugin = NodejsPlugin(tmp_path / "tools")
        with patch.object(plugin, "_latest_matching", return_value="20.17.0") as mock:
            assert plugin._resolve_exact_version("20") == "20.17.0"
        mock.assert_called_once_with(major="20", lts_only=False)

    def test_lts_alias(self, tmp_path: Path) -> None:
        plugin = NodejsPlugin(tmp_path / "tools")
        with patch.object(plugin, "_latest_matching", return_value="20.17.0") as mock:
            assert plugin._resolve_exact_version("lts/*") == "20.17.0"
        mock.assert_called_once_with(lts_only=True)

    def test_range_with_major(self, tmp_path: Path) -> None:
        plugin = NodejsPlugin(tmp_path / "tools")
        with patch.object(plugin, "_latest_matching", return_value="20.17.0") as mock:
            assert plugin._resolve_exact_version(">=20.0.0 <21.0.0") == "20.17.0"
        mock.assert_called_once_with(major="20", lts_only=False)

    def test_unresolvable_raises(self, tmp_path: Path) -> None:
        plugin = NodejsPlugin(tmp_path / "tools")
        with pytest.raises(ToolchainDetectError):
            plugin._resolve_exact_version("not-a-version")


class TestToolchainManager:
    def test_detect_finds_nodejs(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"name": "x"}))
        manager = ToolchainManager(tmp_path / "tools")

        with patch.object(NodejsPlugin, "detect") as mock_detect:
            mock_detect.return_value = ToolchainSpec(
                name="nodejs",
                version="20.17.0",
                install_dir=tmp_path / "tools" / "nodejs" / "20.17.0",
                source_url="https://example.com/node.tar.xz",
            )
            specs = manager.detect(tmp_path)

        assert len(specs) == 1
        assert specs[0].name == "nodejs"

    def test_detect_returns_empty_when_no_plugins_match(self, tmp_path: Path) -> None:
        manager = ToolchainManager(tmp_path / "tools")
        assert manager.detect(tmp_path) == []

    def test_ensure_skips_already_installed(self, tmp_path: Path) -> None:
        install_dir = tmp_path / "tools" / "nodejs" / "20.17.0"
        bin_dir = install_dir / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "node").write_text("#!/bin/sh\n")

        spec = ToolchainSpec(
            name="nodejs",
            version="20.17.0",
            install_dir=install_dir,
            source_url="https://example.com/node.tar.xz",
        )
        manager = ToolchainManager(tmp_path / "tools")

        with patch.object(NodejsPlugin, "install") as mock_install:
            env = manager.ensure([spec])

        mock_install.assert_not_called()
        assert str(bin_dir) in env["PATH"]

    def test_ensure_installs_when_missing(self, tmp_path: Path) -> None:
        install_dir = tmp_path / "tools" / "nodejs" / "20.17.0"
        spec = ToolchainSpec(
            name="nodejs",
            version="20.17.0",
            install_dir=install_dir,
            source_url="https://example.com/node.tar.xz",
        )
        manager = ToolchainManager(tmp_path / "tools")

        with patch.object(NodejsPlugin, "install") as mock_install:
            env = manager.ensure([spec])

        mock_install.assert_called_once_with(spec)
        assert str(install_dir / "bin") in env["PATH"]


class TestEnvWithSystemPath:
    def test_prepends_toolchain_to_system_path(self, monkeypatch) -> None:
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        env = {"PATH": "/data/tools/nodejs/20/bin"}
        merged = env_with_system_path(env)
        assert merged["PATH"].startswith("/data/tools/nodejs/20/bin")
        assert "/usr/bin" in merged["PATH"]


class TestToolchainManagerDisabled:
    def test_disabled_manager_returns_empty_env(self, tmp_path: Path) -> None:
        manager = ToolchainManager(tmp_path / "tools")
        assert manager.ensure([]) == {}


class TestSiblingScratchDirsDoNotCollide:
    """Two patch versions under the same major.minor (e.g. 20.17.0 and
    20.17.5) must get distinct .tmp/.old/.extract scratch paths.

    ``Path.with_suffix`` treats the trailing ``.0``/``.5`` as a file
    extension and replaces it rather than appending, so both versions used
    to collide on the same sibling directory."""

    def test_sibling_appends_rather_than_replaces(self, tmp_path: Path) -> None:
        from issue_agent.runner.toolchains._utils import _sibling

        v1 = tmp_path / "nodejs" / "20.17.0"
        v2 = tmp_path / "nodejs" / "20.17.5"

        assert _sibling(v1, ".tmp") != _sibling(v2, ".tmp")
        assert _sibling(v1, ".tmp").name == "20.17.0.tmp"
        assert _sibling(v2, ".tmp").name == "20.17.5.tmp"
        assert _sibling(v1, ".old").name == "20.17.0.old"
        assert _sibling(v1, ".extract").name == "20.17.0.extract"

    def test_download_and_install_scratch_dirs_do_not_collide(self, tmp_path: Path) -> None:
        from issue_agent.runner.toolchains._utils import download_and_install

        tools_root = tmp_path / "tools" / "nodejs"
        spec_a = ToolchainSpec(
            name="nodejs",
            version="20.17.0",
            install_dir=tools_root / "20.17.0",
            source_url="https://example.com/node-a.tar.xz",
        )
        spec_b = ToolchainSpec(
            name="nodejs",
            version="20.17.5",
            install_dir=tools_root / "20.17.5",
            source_url="https://example.com/node-b.tar.xz",
        )

        def _fake_download(url: str, dest, timeout: float = 120.0) -> None:
            dest.write_bytes(b"archive-bytes")

        def _fake_extract(archive_path, dest_dir, strip_components: int = 1) -> None:
            # Marker file name mirrors the source archive, so cross-talk
            # between the two installs' scratch dirs would show up as the
            # wrong marker landing in the wrong install_dir.
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / "bin").mkdir(parents=True, exist_ok=True)
            (dest_dir / "bin" / archive_path.name).write_bytes(b"marker")

        with (
            patch("issue_agent.runner.toolchains._utils.download_file", side_effect=_fake_download),
            patch(
                "issue_agent.runner.toolchains._utils.extract_archive", side_effect=_fake_extract
            ),
        ):
            download_and_install(spec_a)
            download_and_install(spec_b)

        assert [p.name for p in (spec_a.install_dir / "bin").iterdir()] == ["node-a.tar.xz"]
        assert [p.name for p in (spec_b.install_dir / "bin").iterdir()] == ["node-b.tar.xz"]
