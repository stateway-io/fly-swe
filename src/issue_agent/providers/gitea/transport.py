"""Gitea's ``GitTransport`` (PRD-Gitea §4.5): git over HTTPS with the bot's
access token, and Gitea's URL and reference syntax.

``host`` may carry a subpath (``https://example.com/git``); every URL is
built from it, and git's URL-matched config keys use it as their prefix.
"""

from __future__ import annotations

import base64
from typing import Literal
from urllib.parse import quote, urlsplit

from issue_agent.providers.base import GitTransport


def git_auth_env(
    *, host: str, bot_login: str, token: str, ca_bundle_path: str = ""
) -> dict[str, str]:
    """Env vars that authenticate one git process against ``host``.

    Same technique as the GitHub transport: ``http.<url>.extraHeader`` from
    ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_n``/``GIT_CONFIG_VALUE_n``, so the
    token is never in the remote URL, in ``.git/config`` or on a command
    line, and git sends it only to URLs under ``{host}/``. Gitea takes the
    token as the Basic password (measured with ``capture.py``).

    With ``ca_bundle_path`` set, ``http.<url>.sslCAInfo`` points git at it
    for this host too — the API client trusts it, and a push to an instance
    behind a private CA would otherwise fail.

    Assumes the worker doesn't already export ``GIT_CONFIG_COUNT`` (nothing
    in this codebase sets it).
    """
    prefix = f"http.{host}/"
    credential = base64.b64encode(f"{bot_login}:{token}".encode()).decode()
    config = [(f"{prefix}.extraHeader", f"Authorization: Basic {credential}")]
    if ca_bundle_path:
        config.append((f"{prefix}.sslCAInfo", ca_bundle_path))
    env = {"GIT_CONFIG_COUNT": str(len(config)), "GIT_TERMINAL_PROMPT": "0"}
    for i, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


class GiteaTransport(GitTransport):
    def __init__(self, host: str, *, bot_login: str, token: str, ca_bundle_path: str = "") -> None:
        self._host = host
        self._bot_login = bot_login
        self._token = token
        self._ca_bundle_path = ca_bundle_path

    def clone_url(self, repo: str) -> str:
        return f"{self._host}/{repo}.git"

    def git_auth_env(self, repo: str) -> dict[str, str]:
        # One token per instance: the same credential for every repo.
        return git_auth_env(
            host=self._host,
            bot_login=self._bot_login,
            token=self._token,
            ca_bundle_path=self._ca_bundle_path,
        )

    def pr_url(self, repo: str, number: int) -> str:
        return f"{self._host}/{repo}/pulls/{number}"

    def compare_url(self, repo: str, base: str, head: str) -> str:
        return f"{self._host}/{repo}/compare/{quote(base, safe='/')}...{quote(head, safe='/')}"

    def format_ref(
        self, kind: Literal["issue", "pr"], number: int, *, repo: str, from_repo: str
    ) -> str:
        # Issues and PRs share one index per repo and one syntax.
        return f"#{number}" if repo == from_repo else f"{repo}#{number}"

    def closing_reference(self, issue_repo: str, number: int, target_repo: str) -> str | None:
        # Gitea resolves cross-repo closing keywords on the same instance.
        ref = self.format_ref("issue", number, repo=issue_repo, from_repo=target_repo)
        return f"Closes {ref}"

    def default_commit_email(self, name: str) -> str:
        # Gitea's default NO_REPLY_ADDRESS is noreply.<domain>.
        return f"{name}@noreply.{urlsplit(self._host).hostname}"
