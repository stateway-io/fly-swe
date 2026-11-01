from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from issue_agent.logging import get_logger

if TYPE_CHECKING:
    from issue_agent.config import Settings
    from issue_agent.providers.base import GitTransport

logger = get_logger(__name__)


class Workspace:
    def __init__(
        self,
        task_id: str,
        target_repo: str,
        settings: Settings,
        transport: GitTransport,
    ) -> None:
        # The slug is opaque (`owner/name`, or a nested group path on other
        # forges); it becomes nested directories under the task's dir, so a
        # `..` or absolute part could escape it.
        parts = PurePosixPath(target_repo).parts
        if not parts or PurePosixPath(target_repo).is_absolute() or ".." in parts:
            raise ValueError(f"invalid repo slug: {target_repo!r}")
        self._task_id = task_id
        self._target_repo = target_repo
        self._repo_parts = parts
        self._settings = settings
        self._transport = transport

    @property
    def path(self) -> Path:
        return Path(self._settings.data_dir, "workspaces", self._task_id, *self._repo_parts)

    @property
    def _remote_url(self) -> str:
        # Deliberately tokenless (A2): git persists whatever URL it's given
        # into .git/config, so a tokened URL here would leak the write
        # credential to anything that can read the cloned workspace — the
        # agent included. Authentication for the clone itself travels via
        # the transport's git_auth_env(), which never touches disk.
        return self._transport.clone_url(self._target_repo)

    def clone(self, target_branch: str = "main") -> Path:
        ws_path = self.path

        if ws_path.exists():
            logger.info(
                "workspace_already_exists",
                path=str(ws_path),
                task_id=self._task_id,
            )
            shutil.rmtree(ws_path, ignore_errors=True)

        ws_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(
            "cloning_repo",
            repo=self._target_repo,
            branch=target_branch,
            task_id=self._task_id,
        )

        # Full history by default (git_clone_depth == 0). The agent's
        # "already fixed" verdict has to cite the commit that landed the fix,
        # which it finds with `git log -S` — a --depth=1 clone has nothing to
        # search, and the agent cannot deepen it itself (no token in its env,
        # GIT_TERMINAL_PROMPT=0 — see runner/environment.py). A positive depth
        # bounds the clone on large repos, at the cost of that lookup coming
        # back empty for anything older than the boundary.
        clone_args = ["git", "clone"]
        if self._settings.git_clone_depth > 0:
            clone_args.append(f"--depth={self._settings.git_clone_depth}")
        clone_args += ["--branch", target_branch, self._remote_url, str(ws_path)]

        subprocess.run(
            clone_args,
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, **self._transport.git_auth_env(self._target_repo)},
        )

        subprocess.run(
            ["git", "config", "user.name", self._settings.git_user_name],
            check=True,
            capture_output=True,
            text=True,
            cwd=ws_path,
        )
        subprocess.run(
            ["git", "config", "user.email", self._commit_email()],
            check=True,
            capture_output=True,
            text=True,
            cwd=ws_path,
        )

        logger.info(
            "repo_cloned",
            path=str(ws_path),
            task_id=self._task_id,
        )
        return ws_path

    def _commit_email(self) -> str:
        return self._settings.git_user_email or self._transport.default_commit_email(
            self._settings.git_user_name
        )

    def cleanup(self) -> None:
        ws_path = self.path
        if ws_path.exists():
            shutil.rmtree(ws_path, ignore_errors=True)
            logger.info(
                "workspace_cleaned",
                path=str(ws_path),
                task_id=self._task_id,
            )
