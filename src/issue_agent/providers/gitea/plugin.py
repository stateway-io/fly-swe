"""``GiteaProviderPlugin`` — one Gitea instance (PRD-Gitea §4).

A bot user with a scoped access token; one token per instance, so nothing
is resolved per repo or per delivery.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Self

from issue_agent.core.models import Author
from issue_agent.providers.base import (
    ProviderCapabilities,
    ProviderHealth,
    ProviderPlugin,
    ProviderTerms,
)
from issue_agent.providers.gitea.api import GiteaAPI
from issue_agent.providers.gitea.client import GiteaForgeClient
from issue_agent.providers.gitea.pr import GiteaPullRequestClient
from issue_agent.providers.gitea.transport import GiteaTransport
from issue_agent.providers.gitea.webhook import GiteaWebhookAdapter

if TYPE_CHECKING:
    import httpx

    from issue_agent.config import Settings
    from issue_agent.providers.gitea.settings import GiteaProviderSettings


class GiteaProviderPlugin(ProviderPlugin):
    type = "gitea"
    capabilities = ProviderCapabilities(
        # No draft flag on CreatePullRequestOption; the core prefixes the
        # title with Gitea's default WIP_PREFIXES entry.
        draft_mode="title_prefix",
        draft_prefix="WIP: ",
        supports_pr_api=True,
        supports_labels=True,
        # Gitea Actions commit statuses are a follow-up (PRD-Gitea §9).
        supports_ci=False,
        auto_close_keyword="Closes",
        # No documented API limit; conservative (PRD-Gitea §3.3, §10 Q7).
        max_body_length=65_000,
        terms=ProviderTerms(
            issue="issue",
            pr="Pull Request",
            ref_prefix_issue="#",
            ref_prefix_pr="#",
            # Gitea has no instructions-file convention of its own.
            repo_instructions_files=[],
        ),
    )

    def __init__(
        self,
        instance_id: str,
        config: GiteaProviderSettings,
        *,
        label_agent_generated: str = "agent-generated",
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.instance_id = instance_id
        self._config = config
        self._label_agent_generated = label_agent_generated
        self._http_transport = http_transport
        self._api: GiteaAPI | None = None
        self._api_lock = threading.Lock()
        self.webhook = GiteaWebhookAdapter(
            instance_id=instance_id, secret=config.webhook_secret, bot_login=config.bot_login
        )

    @classmethod
    def from_settings(
        cls, instance_id: str, config: GiteaProviderSettings, settings: Settings
    ) -> Self:
        return cls(instance_id, config, label_agent_generated=settings.pr_label_agent_generated)

    def _get_api(self) -> GiteaAPI:
        # Built on first outbound use, not at startup: the web process only
        # needs the webhook adapter, and a bad ca_bundle_path must show up in
        # /readyz (health_check) rather than stop the process.
        with self._api_lock:
            if self._api is None:
                self._api = GiteaAPI(self._config, transport=self._http_transport)
            return self._api

    def bot_identity(self) -> Author:
        return Author(login=self._config.bot_login, association="bot", is_self=True)

    def client(self, repo: str) -> GiteaForgeClient:
        return GiteaForgeClient(
            self._get_api(),
            repo,
            bot_login=self._config.bot_login,
            author_permission=self._config.author_permission,
        )

    def pulls(self, repo: str) -> GiteaPullRequestClient:
        return GiteaPullRequestClient(
            self._get_api(), repo, label_agent_generated=self._label_agent_generated
        )

    def transport(self) -> GiteaTransport:
        return GiteaTransport(
            self._config.host,
            bot_login=self._config.bot_login,
            token=self._config.token,
            ca_bundle_path=self._config.ca_bundle_path,
        )

    def health_check(self) -> ProviderHealth:
        """From config and the local filesystem only — no network."""
        key = f"providers.{self.instance_id}"
        env = f"PROVIDERS__{self.instance_id.upper()}__"
        config = self._config
        missing = [
            name
            for name, value in (
                (f"{key}.host", config.host),
                (f"{key}.bot_login", config.bot_login),
                (f"{env}TOKEN", config.token),
                (f"{env}WEBHOOK_SECRET", config.webhook_secret),
            )
            if not value
        ]
        config_check = f"missing: {', '.join(missing)}" if missing else "ok"

        tls = "ok"
        if config.ca_bundle_path and not Path(config.ca_bundle_path).is_file():
            tls = f"missing: {key}.ca_bundle_path ({config.ca_bundle_path})"

        author_permission = (
            "warning: api mode needs the bot to be repo admin"
            if config.author_permission == "api"
            else "ok"
        )
        return ProviderHealth(
            ok=config_check == "ok" and tls == "ok",
            checks={"config": config_check, "tls": tls, "author_permission": author_permission},
        )
