"""LiteLLM-backed checks about the configured model — provider resolution and
function-calling support.

The provider is always a parameter here: an explicit ``llm.provider`` from the
config, or the prefix LiteLLM parses out of the model string. It is never
inferred from the shape of the API key.
"""

from __future__ import annotations

import litellm

from issue_agent.logging import get_logger

logger = get_logger(__name__)


def resolve_provider(model: str, explicit_provider: str) -> str:
    """The LiteLLM provider for ``model``: the explicit config value if set,
    otherwise the prefix LiteLLM derives from the model string. Empty string
    when neither is available."""
    if explicit_provider:
        return explicit_provider
    try:
        return litellm.get_llm_provider(model=model)[1]
    except Exception:
        return ""


def check_function_calling(model: str, provider: str) -> str | None:
    """``None`` when the model supports tool/function calling (the harness
    speaks to the model exclusively through ``BASH_TOOL`` — §3.2c), else a
    short advisory string.

    Advisory only: LiteLLM's capability map has false negatives for models it
    doesn't know, so a warning here must never gate readiness.
    """
    try:
        supported = litellm.supports_function_calling(
            model=model, custom_llm_provider=provider or None
        )
    except Exception as exc:
        return (
            f"could not verify function-calling support for {model!r} "
            f"({exc}); the agent requires it"
        )
    if supported:
        return None
    return (
        f"LiteLLM reports {model!r} may not support function calling — "
        f"the agent drives the model through a bash tool and needs it"
    )
