"""``GitHubProviderSettings`` — one GitHub App instance's config (PRD §3.5).

Non-secret fields may come from ``config.yaml`` (``providers.<id>``); the
secrets only from env (``PROVIDERS__<ID>__APP_PRIVATE_KEY``,
``..._APP_PRIVATE_KEY_PATH``, ``..._WEBHOOK_SECRET``). Kept free of PyGithub
so loading the config never imports a provider SDK.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import field_validator, model_validator

from issue_agent.providers.base import ProviderSettingsBase


class GitHubProviderSettings(ProviderSettingsBase):
    secret_fields: ClassVar[frozenset[str]] = frozenset(
        {"app_private_key", "app_private_key_path", "webhook_secret"}
    )

    type: Literal["github"]
    app_id: int = 0
    # The App's bot account (e.g. `my-app[bot]`). Optional: falls back to
    # slugify(agent.name)+"[bot]", but operators should set it so the
    # own-bot filter keeps working even when performed_via_github_app is
    # absent or malformed.
    bot_login: str = ""
    host: str = "https://github.com"
    token_refresh_margin_seconds: int = 300
    app_private_key: str = ""
    app_private_key_path: str = ""
    webhook_secret: str = ""

    @field_validator("host")
    @classmethod
    def _normalize_host(cls, value: str) -> str:
        """Scheme required, trailing slash dropped: the host is concatenated
        into clone URLs and is the key of git's ``http.<url>.extraHeader``,
        which only attaches the credential to URLs it prefixes."""
        host = value.strip().rstrip("/")
        if not host.startswith(("http://", "https://")):
            raise ValueError(
                f"providers.<id>.host must include the scheme, e.g. "
                f"https://ghe.example.com (got {value!r})"
            )
        return host

    @model_validator(mode="after")
    def _load_private_key_from_path(self) -> GitHubProviderSettings:
        if not self.app_private_key and self.app_private_key_path:
            # Imported here: config imports this module.
            from issue_agent.config import resolve_secret_path

            self.app_private_key = resolve_secret_path(self.app_private_key_path).read_text()
        return self

    @property
    def api_url(self) -> str:
        """REST API base for ``host``: github.com and GHE.com
        (``<tenant>.ghe.com``) serve it on an ``api.`` host; GitHub Enterprise
        Server serves it under ``/api/v3``."""
        scheme, _, authority = self.host.partition("://")
        if authority == "github.com":
            return "https://api.github.com"
        if authority.endswith(".ghe.com"):
            return f"{scheme}://api.{authority}"
        return f"{self.host}/api/v3"
