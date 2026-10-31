"""Typed per-instance provider config (PRD §3.5): the discriminated union
``Settings.providers`` validates against. One entry per built-in type, next
to ``BUILTIN_PROVIDER_TYPES``; the modules here never import a provider SDK.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Union

from pydantic import Field

from issue_agent.providers.github.settings import GitHubProviderSettings

if TYPE_CHECKING:
    from issue_agent.providers.base import ProviderSettingsBase

SETTINGS_BY_TYPE: dict[str, type[ProviderSettingsBase]] = {
    "github": GitHubProviderSettings,
}

ProviderSettings = Annotated[
    Union[GitHubProviderSettings],  # noqa: UP007 — a union of one, for now
    Field(discriminator="type"),
]
