"""GitHub shape -> canonical model (PRD §3.1).

The single place that knows how GitHub spells an author: ``user.login``,
``user.type``, ``author_association`` and ``performed_via_github_app``.
Shared by the webhook parser (raw payloads) and the API client (comment
history), so both produce the same ``Author``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from issue_agent.core.models import Association, Author

if TYPE_CHECKING:
    from issue_agent.providers.github.settings import GitHubProviderSettings


@dataclass(frozen=True)
class BotIdentity:
    """Who "this service's own bot" is on GitHub."""

    app_id: int
    app_name: str
    app_bot_login: str | None = None

    @property
    def bot_login(self) -> str:
        return _derive_bot_login(self.app_name, self.app_bot_login)


def bot_identity_from_settings(config: GitHubProviderSettings, agent_name: str) -> BotIdentity:
    return BotIdentity(
        app_id=config.app_id,
        app_name=agent_name,
        app_bot_login=config.bot_login,
    )


_ASSOCIATIONS: dict[str, Association] = {
    "OWNER": "owner",
    "MEMBER": "member",
    "COLLABORATOR": "collaborator",
    "CONTRIBUTOR": "contributor",
    # First commit to this repo / first commit on GitHub at all: GitHub's
    # finer grades collapse into the canonical scale.
    "FIRST_TIME_CONTRIBUTOR": "contributor",
    "FIRST_TIMER": "none",
    "MANNEQUIN": "none",
    "NONE": "none",
}


def map_association(raw: str | None) -> Association:
    """GitHub ``author_association`` -> canonical association.

    Absent or unrecognized values are ``unknown`` rather than ``none``: only
    a value GitHub actually sent may decide a filter at ingestion.
    """
    return _ASSOCIATIONS.get((raw or "").strip().upper(), "unknown")


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _normalize_app_id(value: Any) -> int | None:
    """Coerce an app id to int, tolerating string payloads from proxies/GitHub."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _derive_bot_login(app_name: str, app_bot_login: str | None) -> str:
    """Return the expected bot account login for this App.

    Operators can set ``providers.<id>.bot_login`` explicitly; otherwise we fall
    back to slugifying the configured app name. The fallback is brittle when
    the GitHub App slug differs from ``agent.name`` (e.g. public slug
    ``acme-issue-agent`` vs configured name ``issue-agent``), so explicit
    configuration is recommended.
    """
    if app_bot_login:
        return app_bot_login.strip().lower()
    return f"{_slugify(app_name)}[bot]"


def comment_dict_is_own_app(
    *,
    login: str | None,
    user_type: str | None,
    performed_via_github_app_id: Any,
    app_id: int,
    app_name: str,
    app_bot_login: str | None = None,
) -> bool:
    """True when a comment (given as loose fields) was authored by *this* App.

    Other bots are intentionally *not* matched — they may legitimately drive a
    conversation with the agent. Shared by the webhook parser (raw payload)
    and the run-context assembly (normalized comment dicts, R3.3).
    """
    app_id_int = _normalize_app_id(app_id)
    performed_id = _normalize_app_id(performed_via_github_app_id)
    if app_id_int and performed_id == app_id_int:
        return True
    if (user_type or "") != "Bot":
        return False
    return (login or "").lower() == _derive_bot_login(app_name, app_bot_login)


def author_from_github(
    user: dict[str, Any] | None,
    *,
    association: str | None,
    performed_via_github_app_id: Any,
    identity: BotIdentity,
) -> Author:
    """Build a canonical ``Author`` from a GitHub ``user`` object plus the
    association/app fields GitHub reports alongside it."""
    user = user or {}
    login = user.get("login") or ""
    return Author(
        login=login,
        association=map_association(association),
        is_self=comment_dict_is_own_app(
            login=login,
            user_type=user.get("type"),
            performed_via_github_app_id=performed_via_github_app_id,
            app_id=identity.app_id,
            app_name=identity.app_name,
            app_bot_login=identity.app_bot_login,
        ),
    )
