"""``GitHubProviderPlugin`` — a GitHub App instance (PRD §3.4)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from issue_agent.core.models import Author
from issue_agent.providers.base import (
    ProviderCapabilities,
    ProviderHealth,
    ProviderPlugin,
    ProviderTerms,
)
from issue_agent.providers.github.auth import GitHubAppAuth
from issue_agent.providers.github.client import GitHubForgeClient
from issue_agent.providers.github.normalize import bot_identity_from_settings
from issue_agent.providers.github.pr import GitHubPullRequestClient
from issue_agent.providers.github.transport import GitHubTransport
from issue_agent.providers.github.webhook import GitHubWebhookAdapter

if TYPE_CHECKING:
    from collections.abc import Callable

    from issue_agent.config import Settings
    from issue_agent.providers.github.normalize import BotIdentity
    from issue_agent.providers.github.settings import GitHubProviderSettings


class GitHubProviderPlugin(ProviderPlugin):
    type = "github"
    capabilities = ProviderCapabilities(
        draft_mode="native",
        supports_pr_api=True,
        supports_labels=True,
        auto_close_keyword="Closes",
        # GitHub rejects issue/PR bodies above 65,536 chars; leave headroom.
        max_body_length=58_000,
        terms=ProviderTerms(
            issue="issue",
            pr="Pull Request",
            ref_prefix_issue="#",
            ref_prefix_pr="#",
            repo_instructions_files=[".github/copilot-instructions.md"],
        ),
    )

    def __init__(
        self,
        instance_id: str,
        *,
        webhook_secret: str,
        identity: BotIdentity,
        host: str = "https://github.com",
        private_key: str = "",
        label_agent_generated: str = "agent-generated",
        auth_factory: Callable[[], GitHubAppAuth] | None = None,
    ) -> None:
        self.instance_id = instance_id
        self._identity = identity
        self._webhook_secret = webhook_secret
        self._host = host
        self._private_key = private_key
        self._label_agent_generated = label_agent_generated
        self._auth_factory = auth_factory
        self._auth: GitHubAppAuth | None = None
        self.webhook = GitHubWebhookAdapter(
            instance_id=instance_id, secret=webhook_secret, identity=identity
        )

    @classmethod
    def from_settings(
        cls, instance_id: str, config: GitHubProviderSettings, settings: Settings
    ) -> Self:
        return cls(
            instance_id,
            webhook_secret=config.webhook_secret,
            identity=bot_identity_from_settings(config, settings.agent_name),
            host=config.host,
            private_key=config.app_private_key,
            label_agent_generated=settings.pr_label_agent_generated,
            auth_factory=lambda: GitHubAppAuth(config),
        )

    def _get_auth(self) -> GitHubAppAuth:
        # Built on first outbound use, not at startup: the web process only
        # ever needs the webhook adapter.
        if self._auth is None:
            if self._auth_factory is None:
                raise RuntimeError(f"provider {self.instance_id!r} has no GitHub App credentials")
            self._auth = self._auth_factory()
        return self._auth

    def bot_identity(self) -> Author:
        return Author(login=self._identity.bot_login, association="bot", is_self=True)

    def client(self, repo: str) -> GitHubForgeClient:
        return GitHubForgeClient(self._get_auth(), repo, self._identity)

    def pulls(self, repo: str) -> GitHubPullRequestClient:
        return GitHubPullRequestClient(
            self._get_auth(), repo, label_agent_generated=self._label_agent_generated
        )

    def transport(self) -> GitHubTransport:
        return GitHubTransport(self._get_auth(), self._host)

    def health_check(self) -> ProviderHealth:
        env = f"PROVIDERS__{self.instance_id.upper()}__"
        missing = []
        if not self._identity.app_id:
            missing.append(f"providers.{self.instance_id}.app_id")
        if not self._webhook_secret:
            missing.append(f"{env}WEBHOOK_SECRET")
        config = f"missing: {', '.join(missing)}" if missing else "ok"

        if not self._private_key:
            auth = "private key not loaded"
        elif not self._private_key.startswith("-----BEGIN"):
            auth = "private key does not look like a valid PEM"
        else:
            auth = "ok"

        # Informational only: without an explicit bot login, own-comment
        # detection falls back to slugify(agent.name), which can differ from
        # the App's real bot login (PR #74).
        bot_login = (
            "ok"
            if self._identity.app_bot_login
            else (
                f"warning: providers.{self.instance_id}.bot_login is not set — own-bot comment "
                "filtering falls back to slugify(agent.name), which can mismatch "
                "the real GitHub App bot login"
            )
        )
        return ProviderHealth(
            ok=config == "ok" and auth == "ok",
            checks={"config": config, "auth": auth, "own_bot_login": bot_login},
        )
