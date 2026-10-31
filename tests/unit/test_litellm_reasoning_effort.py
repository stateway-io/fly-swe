"""Contract test for LiteLLM's ``reasoning_effort`` -> provider mapping.

`_model_kwargs()` (runner/agent_factory.py) hands LiteLLM a single
``reasoning_effort`` string and relies on LiteLLM itself to translate it into
the provider's native reasoning shape. This file locks the three behaviors
the feature depends on, against the pinned LiteLLM version, with no network
call (``get_optional_params`` is pure request-shaping):

1. On an adaptive-thinking Anthropic model, it becomes
   ``thinking={"type": "adaptive"}`` + ``output_config.effort`` — the modern
   control surface those models require (they reject a fixed
   ``budget_tokens``).
2. On an older Anthropic model, it becomes a *fixed* thinking-token budget
   instead (there is no ``output_config.effort`` to use).
3. On a model/provider with no reasoning support, LiteLLM raises
   ``UnsupportedParamsError`` — and that exception type is one of
   mini-swe-agent's ``abort_exceptions``, which is what turns an
   unsupported-model misconfiguration into a clean task abort instead of ten
   retries against a guaranteed-to-fail request.

If a LiteLLM upgrade changes any of this, it should fail here first.
"""

from __future__ import annotations

import litellm
import pytest
from minisweagent.models.litellm_model import LitellmModel


def test_adaptive_model_maps_to_thinking_adaptive_and_output_config_effort():
    params = litellm.utils.get_optional_params(
        model="claude-opus-5-5",
        custom_llm_provider="anthropic",
        reasoning_effort="high",
    )
    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"] == {"effort": "high"}


def test_legacy_model_maps_to_a_fixed_thinking_budget():
    params = litellm.utils.get_optional_params(
        model="claude-sonnet-4-5",
        custom_llm_provider="anthropic",
        reasoning_effort="high",
    )
    assert params["thinking"]["type"] == "enabled"
    assert isinstance(params["thinking"]["budget_tokens"], int)
    assert "output_config" not in params


def test_unsupported_model_raises_unsupported_params_error():
    with pytest.raises(litellm.exceptions.UnsupportedParamsError):
        litellm.utils.get_optional_params(
            model="gpt-4o",
            custom_llm_provider="openai",
            reasoning_effort="high",
        )


def test_unsupported_params_error_is_a_minisweagent_abort_exception():
    # The whole point of letting LiteLLM raise on an unsupported model is
    # that mini-swe-agent treats it as fatal rather than retrying it.
    assert litellm.exceptions.UnsupportedParamsError in LitellmModel.abort_exceptions
