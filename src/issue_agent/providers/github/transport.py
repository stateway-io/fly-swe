from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Literal

from issue_agent.providers.base import GitTransport

if TYPE_CHECKING:
    from issue_agent.providers.github.auth import GitHubAppAuth


def git_auth_env(token: str, *, host: str) -> dict[str, str]:
    """Env vars that authenticate one git invocation without persisting anything.

    git reads `http.<host>.extraHeader` from `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/
    `GIT_CONFIG_VALUE_0` for the lifetime of the process that inherits this
    env — never written to `.git/config`, and (unlike `git -c key=val`, which
    puts the credential on the command line) not visible in `ps`.

    The header is scoped to `host` (a URL-matched `http.<url>.*` config key,
    not the bare `http.extraHeader`): git only attaches it to requests whose
    URL matches that prefix, so the token isn't handed to every host the
    git process happens to contact (a second remote, a submodule, an LFS
    endpoint) — only to the one it's actually for. ``host`` has no default
    on purpose: a caller that forgets it would silently authenticate
    github.com only, which is how GitHub Enterprise went unnoticed.

    Fixes A2's second half: with the remote URL always tokenless
    (`Workspace._remote_url`), `.git/config` in the agent's workspace never
    contains a credential, at any point the agent can read it.

    Assumes the worker process doesn't already export `GIT_CONFIG_COUNT`
    (true today — nothing in this codebase sets it); if that ever changes,
    this needs to read and increment it instead of hardcoding "1".
    """
    credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{host}.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}",
        "GIT_TERMINAL_PROMPT": "0",
    }


class GitHubTransport(GitTransport):
    """git over HTTPS with an App installation token, and github.com's (or
    a GHE host's) URL and reference syntax."""

    def __init__(self, auth: GitHubAppAuth, host: str) -> None:
        self._auth = auth
        self._host = host

    def clone_url(self, repo: str) -> str:
        return f"{self._host}/{repo}.git"

    def git_auth_env(self, repo: str) -> dict[str, str]:
        return git_auth_env(self._auth.token_for(repo), host=self._host)

    def pr_url(self, repo: str, number: int) -> str:
        return f"{self._host}/{repo}/pull/{number}"

    def compare_url(self, repo: str, base: str, head: str) -> str:
        return f"{self._host}/{repo}/compare/{base}...{head}"

    def format_ref(
        self, kind: Literal["issue", "pr"], number: int, *, repo: str, from_repo: str
    ) -> str:
        # Issues and PRs share one number space and one syntax on GitHub. A
        # bare `#N` in another repo's text points at *that* repo (bug A4).
        if repo != from_repo:
            return f"{repo}#{number}"
        return f"#{number}"

    def closing_reference(self, issue_repo: str, number: int, target_repo: str) -> str | None:
        ref = self.format_ref("issue", number, repo=issue_repo, from_repo=target_repo)
        return f"Closes {ref}"

    def default_commit_email(self, name: str) -> str:
        return f"{name}@users.noreply.github.com"
