from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from issue_agent.runner.deps import install_project_dependencies
from issue_agent.runner.toolchains import ToolchainSpec

if TYPE_CHECKING:
    from pathlib import Path


class TestInstallProjectDependencies:
    def test_no_nodejs_spec_does_nothing(self, tmp_path: Path) -> None:
        install_project_dependencies(tmp_path, [], {}, timeout_seconds=30)
        # No error, no files created
        assert list(tmp_path.iterdir()) == []

    def test_runs_npm_ci_when_package_lock_exists(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"name": "x"}))
        (tmp_path / "package-lock.json").write_text("{}")
        spec = ToolchainSpec(
            name="nodejs",
            version="20.17.0",
            install_dir=tmp_path / "nodejs",
            source_url="https://example.com/node.tar.xz",
        )

        with patch("issue_agent.runner.deps._run_sandboxed") as mock_run:
            mock_run.return_value.stdout = "ok"
            mock_run.return_value.returncode = 0
            install_project_dependencies(
                tmp_path, [spec], {"PATH": "/data/tools/nodejs/bin"}, timeout_seconds=30
            )

        mock_run.assert_called_once()
        assert "npm ci" in mock_run.call_args[0][0]

    def test_falls_back_to_npm_install_without_lock(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"name": "x"}))
        spec = ToolchainSpec(
            name="nodejs",
            version="20.17.0",
            install_dir=tmp_path / "nodejs",
            source_url="https://example.com/node.tar.xz",
        )

        with patch("issue_agent.runner.deps._run_sandboxed") as mock_run:
            mock_run.return_value.stdout = "ok"
            mock_run.return_value.returncode = 0
            install_project_dependencies(
                tmp_path, [spec], {"PATH": "/data/tools/nodejs/bin"}, timeout_seconds=30
            )

        assert "npm install" in mock_run.call_args[0][0]

    def test_failure_is_logged_not_raised(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"name": "x"}))
        spec = ToolchainSpec(
            name="nodejs",
            version="20.17.0",
            install_dir=tmp_path / "nodejs",
            source_url="https://example.com/node.tar.xz",
        )

        with patch("issue_agent.runner.deps._run_sandboxed") as mock_run:
            mock_run.return_value.stdout = "error"
            mock_run.return_value.returncode = 1
            install_project_dependencies(
                tmp_path, [spec], {"PATH": "/data/tools/nodejs/bin"}, timeout_seconds=30
            )

        mock_run.assert_called_once()

    def test_yarn_lock_uses_yarn(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"name": "x"}))
        (tmp_path / "yarn.lock").write_text("")
        spec = ToolchainSpec(
            name="nodejs",
            version="20.17.0",
            install_dir=tmp_path / "nodejs",
            source_url="https://example.com/node.tar.xz",
        )

        with patch("issue_agent.runner.deps._run_sandboxed") as mock_run:
            mock_run.return_value.stdout = "ok"
            mock_run.return_value.returncode = 0
            install_project_dependencies(
                tmp_path, [spec], {"PATH": "/data/tools/nodejs/bin"}, timeout_seconds=30
            )

        assert "yarn install" in mock_run.call_args[0][0]

    def test_pnpm_lock_uses_pnpm(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"name": "x"}))
        (tmp_path / "pnpm-lock.yaml").write_text("")
        spec = ToolchainSpec(
            name="nodejs",
            version="20.17.0",
            install_dir=tmp_path / "nodejs",
            source_url="https://example.com/node.tar.xz",
        )

        with patch("issue_agent.runner.deps._run_sandboxed") as mock_run:
            mock_run.return_value.stdout = "ok"
            mock_run.return_value.returncode = 0
            install_project_dependencies(
                tmp_path, [spec], {"PATH": "/data/tools/nodejs/bin"}, timeout_seconds=30
            )

        assert "pnpm install" in mock_run.call_args[0][0]
