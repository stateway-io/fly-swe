"""Building ``providers.<id>`` config for tests (PRD §3.5)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tests.helpers.github import TEST_SECRET

if TYPE_CHECKING:
    from issue_agent.config import Settings

MOCK_PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\nMOCKKEY\n-----END RSA PRIVATE KEY-----"
)


def github_provider(**fields: Any) -> dict[str, Any]:
    """One GitHub App instance's raw config, as YAML + env would merge it."""
    return {
        "type": "github",
        "app_id": 123,
        "webhook_secret": TEST_SECRET,
        "app_private_key": MOCK_PRIVATE_KEY,
        **fields,
    }


def github_providers(instance_id: str = "github", **fields: Any) -> dict[str, Any]:
    """A ``PROVIDERS=`` value with a single GitHub instance."""
    return {instance_id: github_provider(**fields)}


def with_github(settings: Settings, instance_id: str = "github", **fields: Any) -> Settings:
    """A copy of ``settings`` with fields of one GitHub instance replaced."""
    config = settings.providers[instance_id].model_copy(update=fields)
    return settings.model_copy(
        update={"providers": {**settings.providers, instance_id: config}}
    )
