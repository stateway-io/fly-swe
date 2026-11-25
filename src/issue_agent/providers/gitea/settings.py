"""``GiteaProviderSettings`` — one Gitea instance's config (PRD-Gitea §4.7).

Non-secret fields may come from ``config.yaml`` (``providers.<id>``); the
secrets only from env (``PROVIDERS__<ID>__TOKEN``,
``PROVIDERS__<ID>__WEBHOOK_SECRET``). Imports only pydantic and the ports, so
loading the config never pulls in the HTTP client.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field, field_validator

from issue_agent.providers.base import ProviderSettingsBase


class GiteaProviderSettings(ProviderSettingsBase):
    secret_fields: ClassVar[frozenset[str]] = frozenset({"token", "webhook_secret"})

    type: Literal["gitea"]
    # Required, but checked by health_check rather than at load time, so a
    # half-configured instance reports what's missing instead of crashing
    # the process. No sensible default for a self-hosted forge.
    host: str = ""
    # The bot account's login: the own-comment filter matches on it.
    bot_login: str = ""
    # allowlist: never ask the forge who a commenter is. api: ask
    # /collaborators/{login}/permission, which needs the bot to be repo admin.
    author_permission: Literal["allowlist", "api"] = "allowlist"
    # A PEM bundle (e.g. on the volume) for an instance behind a private CA;
    # empty means the system store. There is deliberately no way to turn
    # verification off.
    ca_bundle_path: str = ""
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    token: str = ""
    webhook_secret: str = ""

    @field_validator("host")
    @classmethod
    def _normalize_host(cls, value: str) -> str:
        """Scheme required, trailing slash dropped: the host is concatenated
        into API and clone URLs and is the key of git's
        ``http.<url>.extraHeader``. A subpath (``https://example.com/git``)
        is kept."""
        host = value.strip().rstrip("/")
        if host and not host.startswith(("http://", "https://")):
            raise ValueError(
                f"providers.<id>.host must include the scheme, e.g. "
                f"https://git.example.com (got {value!r})"
            )
        return host

    @field_validator("bot_login", "ca_bundle_path")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @property
    def api_url(self) -> str:
        return f"{self.host}/api/v1"
