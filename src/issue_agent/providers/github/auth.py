from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from github import Auth, Github, GithubIntegration

if TYPE_CHECKING:
    from issue_agent.providers.github.settings import GitHubProviderSettings


@dataclass
class _CachedToken:
    token: str
    expires_at: float


class GitHubAppAuth:
    def __init__(self, config: GitHubProviderSettings) -> None:
        self._integration = GithubIntegration(
            config.app_id,
            config.app_private_key,
            base_url=config.api_url,
        )
        self._base_url = config.api_url
        self._refresh_margin_seconds = config.token_refresh_margin_seconds
        self._cache: dict[int, _CachedToken] = {}
        self._installations: dict[str, int] = {}

    def get_token(self, installation_id: int) -> str:
        cached = self._cache.get(installation_id)
        if cached and time.time() < cached.expires_at - self._refresh_margin_seconds:
            return cached.token
        return self._fetch_token(installation_id)

    def _fetch_token(self, installation_id: int) -> str:
        auth = self._integration.get_access_token(installation_id)
        token = _CachedToken(
            token=auth.token,
            expires_at=auth.expires_at.timestamp(),
        )
        self._cache[installation_id] = token
        return token.token

    def get_client(self, installation_id: int) -> Github:
        return Github(
            auth=Auth.Token(self.get_token(installation_id)), base_url=self._base_url
        )

    def resolve_installation_id(self, owner: str, repo: str) -> int:
        return self._integration.get_repo_installation(owner, repo).id

    def installation_for(self, repo: str) -> int:
        """The App installation that covers ``repo`` (``owner/name``),
        resolved once per process. Each repo gets its own — the issues and
        target repos of a route needn't share an installation (PRD §2.2)."""
        installation_id = self._installations.get(repo)
        if installation_id is None:
            owner, name = repo.split("/", 1)
            installation_id = self.resolve_installation_id(owner, name)
            self._installations[repo] = installation_id
        return installation_id

    def token_for(self, repo: str) -> str:
        return self.get_token(self.installation_for(repo))

    def client_for(self, repo: str) -> Github:
        return self.get_client(self.installation_for(repo))

