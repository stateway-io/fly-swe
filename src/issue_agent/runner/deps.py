from __future__ import annotations

from typing import TYPE_CHECKING

from issue_agent.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path
from issue_agent.runner.environment import _run as _run_sandboxed
from issue_agent.runner.environment import _sandboxed_env
from issue_agent.runner.toolchains.manager import env_with_system_path

if TYPE_CHECKING:
    from issue_agent.runner.toolchains.spec import ToolchainSpec

logger = get_logger(__name__)


def install_project_dependencies(
    workspace_path: Path,
    specs: list[ToolchainSpec],
    toolchain_env: dict[str, str],
    *,
    timeout_seconds: int = 300,
) -> None:
    """Best-effort installation of project dependencies before the agent runs.

    Failures are logged but not raised — the agent can still attempt to
    install dependencies itself and the harness test command can retry.
    """
    if not any(s.name == "nodejs" for s in specs):
        return

    env = env_with_system_path(toolchain_env)

    if (workspace_path / "pnpm-lock.yaml").exists():
        _run_best_effort(workspace_path, ["pnpm", "install", "--frozen-lockfile"], env, timeout_seconds)
    elif (workspace_path / "yarn.lock").exists():
        _run_best_effort(workspace_path, ["yarn", "install", "--frozen-lockfile"], env, timeout_seconds)
    elif (workspace_path / "package-lock.json").exists():
        _run_best_effort(workspace_path, ["npm", "ci"], env, timeout_seconds)
    elif (workspace_path / "package.json").exists():
        _run_best_effort(workspace_path, ["npm", "install"], env, timeout_seconds)


def _run_best_effort(
    workspace_path: Path,
    command: list[str],
    env: dict[str, str],
    timeout_seconds: int,
) -> None:
    cmd_str = " ".join(command)
    logger.info("installing_project_dependencies", command=cmd_str, workspace=str(workspace_path))
    try:
        result = _run_sandboxed(cmd_str, str(workspace_path), _sandboxed_env(env), timeout_seconds)
        if result.returncode == 0:
            logger.info("project_dependencies_installed", command=cmd_str)
        else:
            logger.warning(
                "project_dependencies_install_failed",
                command=cmd_str,
                returncode=result.returncode,
                output=result.stdout[-500:],
            )
    except Exception as exc:
        logger.warning("project_dependencies_install_error", command=cmd_str, error=str(exc))
