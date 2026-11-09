"""Provider types and the configured instances (PRD §3.3).

Types are static — a new forge is one entry in ``BUILTIN_PROVIDER_TYPES``
(and its settings model in ``providers/settings.py``). Instances come from
config (``providers.<id>``). The registry is an injected object
(``app.state.registry``, ``worker.main()``), never a global.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from issue_agent.providers.base import ProviderPlugin
from issue_agent.providers.github.plugin import GitHubProviderPlugin

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from issue_agent.config import Settings

BUILTIN_PROVIDER_TYPES: dict[str, type[ProviderPlugin]] = {
    GitHubProviderPlugin.type: GitHubProviderPlugin,
}


class ProviderRegistry:
    def __init__(self, plugins: Mapping[str, ProviderPlugin]) -> None:
        for instance_id, plugin in plugins.items():
            if plugin.instance_id != instance_id:
                raise ValueError(
                    f"provider registered as {instance_id!r} says it is {plugin.instance_id!r}"
                )
            has_pulls = type(plugin).pulls is not ProviderPlugin.pulls
            if has_pulls != plugin.capabilities.supports_pr_api:
                raise ValueError(
                    f"provider {instance_id!r} declares supports_pr_api="
                    f"{plugin.capabilities.supports_pr_api} but "
                    f"{'implements' if has_pulls else 'does not implement'} pulls()"
                )
            has_ci = type(plugin).ci is not ProviderPlugin.ci
            if has_ci != plugin.capabilities.supports_ci:
                raise ValueError(
                    f"provider {instance_id!r} declares supports_ci="
                    f"{plugin.capabilities.supports_ci} but "
                    f"{'implements' if has_ci else 'does not implement'} ci()"
                )
        self._plugins = dict(plugins)

    @classmethod
    def from_settings(cls, settings: Settings) -> ProviderRegistry:
        return cls(
            {
                instance_id: BUILTIN_PROVIDER_TYPES[config.type].from_settings(
                    instance_id, config, settings
                )
                for instance_id, config in settings.providers.items()
            }
        )

    def get(self, instance_id: str) -> ProviderPlugin:
        """The configured instance; ``KeyError`` when there is none."""
        return self._plugins[instance_id]

    def all(self) -> Iterable[ProviderPlugin]:
        return self._plugins.values()
