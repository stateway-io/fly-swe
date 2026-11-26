"""A throwaway Gitea in Docker, for the fixture capture tool and the opt-in
e2e (PRD-Gitea GT1/GT4). Everything it creates is synthetic and local.

Needs Docker and git. The container reaches services on the host through
``host.docker.internal``; the bridge gateway IP is put in Gitea's webhook
``ALLOWED_HOST_LIST`` (Gitea 28 wants an explicit IP there, and 1.27 reads
the ``[webhook]`` key — both are set).
"""

from __future__ import annotations

import base64
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

PASSWORD = "Capture-pass-1234"  # throwaway containers only


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def ok(resp: httpx.Response) -> Any:
    if resp.is_error:
        raise RuntimeError(
            f"{resp.request.method} {resp.request.url} -> {resp.status_code} {resp.text}"
        )
    return resp.json() if resp.content else None


class GiteaServer:
    def __init__(self, *, name: str, port: int, root_url: str | None = None) -> None:
        self.name = name
        self.port = port
        self.url = f"http://localhost:{port}"
        self.root_url = root_url or f"{self.url}/"

    # --- Lifecycle -----------------------------------------------------------

    def start(self, image: str) -> None:
        """Start a fresh container (replacing one of the same name) and create
        the site admin ``root``."""
        self.stop()
        gateway = _docker(
            "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"
        ).stdout.strip()
        env = {
            "GITEA__security__INSTALL_LOCK": "true",
            "GITEA__database__DB_TYPE": "sqlite3",
            "GITEA__server__ROOT_URL": self.root_url,
            "GITEA__server__HTTP_PORT": "3000",
            "GITEA__service__DISABLE_REGISTRATION": "true",
            "GITEA__security__ALLOWED_HOST_LIST": gateway,
            "GITEA__webhook__ALLOWED_HOST_LIST": gateway,
            "GITEA__webhook__DELIVER_TIMEOUT": "10",
            "GITEA__repository__DEFAULT_BRANCH": "main",
            "GITEA__log__LEVEL": "Warn",
            # The web login (capture's "Replay") runs over plain http.
            "GITEA__session__COOKIE_SECURE": "false",
        }
        args = ["run", "-d", "--name", self.name, "-p", f"{self.port}:3000"]
        args += ["--add-host", "host.docker.internal:host-gateway"]
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        _docker(*args, image)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.url}/api/healthz", timeout=2).is_success:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        else:
            raise TimeoutError(_docker("logs", self.name, check=False).stderr[-2000:])
        _docker(
            "exec", "-u", "git", self.name, "gitea", "admin", "user", "create", "--admin",
            "--username", "root", "--password", PASSWORD, "--email", "root@example.com",
            "--must-change-password=false",
        )  # fmt: skip

    def stop(self) -> None:
        _docker("rm", "-f", self.name, check=False)

    # --- API -----------------------------------------------------------------

    def api(self, *, user: str | None = None, token: str | None = None) -> httpx.Client:
        """A client as ``user`` (basic auth, the shared password) or with
        ``token``; neither means anonymous."""
        headers = {"Authorization": f"token {token}"} if token else {}
        auth = httpx.BasicAuth(user, PASSWORD) if user else None
        return httpx.Client(base_url=f"{self.url}/api/v1", auth=auth, headers=headers, timeout=30)

    def root(self) -> httpx.Client:
        return self.api(user="root")

    def make_token(self, user: str, name: str, scopes: list[str]) -> str:
        with self.api(user=user) as c:
            return ok(c.post(f"/users/{user}/tokens", json={"name": name, "scopes": scopes}))[
                "sha1"
            ]

    def create_user(self, login: str) -> None:
        with self.root() as root:
            ok(root.post("/admin/users", json={
                "username": login, "email": f"{login}@example.com", "password": PASSWORD,
                "must_change_password": False,
            }))  # fmt: skip

    def add_hook(self, repo: str, *, url: str, secret: str, events: list[str]) -> int:
        with self.root() as root:
            hook = ok(root.post(f"/repos/{repo}/hooks", json={
                "type": "gitea",
                "active": True,
                "branch_filter": "*",
                "events": events,
                "config": {"url": url, "content_type": "json", "secret": secret},
            }))  # fmt: skip
        return hook["id"]

    # --- Git -----------------------------------------------------------------

    def git_push(
        self,
        token: str,
        user: str,
        repo: str,
        branch: str,
        path: str,
        content: str,
        *,
        base: str = "main",
    ) -> int:
        """Clone ``base``, commit one file on ``branch`` and push it with the
        extraHeader technique the transport uses. Returns git's exit code."""
        basic = base64.b64encode(f"{user}:{token}".encode()).decode()
        env = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{self.url}/.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": tempfile.gettempdir(),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        }
        with tempfile.TemporaryDirectory() as tmp:

            def run(*a: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(a, cwd=tmp, env=env, capture_output=True, text=True)

            if run("git", "clone", "-q", "-b", base, f"{self.url}/{repo}.git", ".").returncode:
                return 128
            run("git", "checkout", "-q", "-B", branch)
            Path(tmp, path).write_text(content)
            run("git", "add", path)
            run("git", "-c", "user.name=capture", "-c", "user.email=capture@example.com",
                "commit", "-q", "-m", f"capture: {branch}")  # fmt: skip
            return run("git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}").returncode
