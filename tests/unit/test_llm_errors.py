"""Provider error parsing — the OpenRouter budget guardrail of
An OpenRouter budget refusal arrived as a generic ``litellm.APIError`` whose text
was cut mid-JSON in the abort comment."""

from __future__ import annotations

import json

import litellm

from issue_agent.runner.llm_errors import (
    ProviderBudgetExceededError,
    budget_error,
    clip,
    describe_llm_error,
    parse_provider_error,
)

_BUDGET_BODY = json.dumps(
    {
        "error": {
            "message": (
                "Budget limit exceeded (monthly limit). Contact your org admin. "
                "$10.02 of $10.00 used; resets 2026-11-01T00:00:00.000Z."
            ),
            "code": 403,
            "metadata": {
                "limit_source": "openrouter_guardrail_budget",
                "remedy_hint": "Wait for the budget to reset or ask your org admin to raise it.",
            },
        }
    }
)


def _api_error(message: str, status_code: int = 500) -> litellm.APIError:
    return litellm.APIError(
        status_code=status_code, message=message, llm_provider="openrouter", model="m"
    )


def budget_exc() -> litellm.APIError:
    return _api_error(f"OpenrouterException - {_BUDGET_BODY}")


class TestParseProviderError:
    def test_reads_message_code_and_metadata_from_the_body(self):
        error = parse_provider_error(budget_exc())
        assert error is not None
        assert error.message.startswith("Budget limit exceeded (monthly limit).")
        assert error.code == 403
        assert error.limit_source == "openrouter_guardrail_budget"
        assert error.remedy_hint.startswith("Wait for the budget")
        assert error.resets_at == "2026-11-01T00:00:00.000Z"
        assert error.spent_usd == 10.02
        assert error.limit_usd == 10.0

    def test_trailing_text_after_the_json_is_ignored(self):
        error = parse_provider_error(_api_error(f"OpenrouterException - {_BUDGET_BODY} (retry 3)"))
        assert error is not None and error.code == 403

    def test_without_a_body_falls_back_to_the_text_and_status(self):
        error = parse_provider_error(_api_error("OpenrouterException - boom", status_code=502))
        assert error is not None
        assert error.message == "boom"
        assert error.code == 502


class TestBudgetError:
    def test_openrouter_budget_guardrail_403(self):
        assert budget_error(budget_exc()) is not None

    def test_insufficient_credits_402(self):
        body = json.dumps({"error": {"message": "Insufficient credits", "code": 402}})
        assert budget_error(_api_error(f"OpenrouterException - {body}")) is not None

    def test_plain_403_is_not_a_budget_error(self):
        body = json.dumps({"error": {"message": "Forbidden: model not allowed", "code": 403}})
        assert budget_error(_api_error(f"OpenrouterException - {body}")) is None

    def test_transient_failures_are_not_budget_errors(self):
        assert budget_error(_api_error("OpenrouterException - [SSL: UNEXPECTED_EOF]")) is None
        assert budget_error(TimeoutError("read timed out")) is None


class TestDescribeLlmError:
    def test_budget_error_shows_the_provider_message_not_json(self):
        text = describe_llm_error(budget_exc())
        assert text.startswith("Budget limit exceeded (monthly limit).")
        assert "— Wait for the budget" in text
        assert "{" not in text
        assert "litellm" not in text
        assert "OpenrouterException" not in text

    def test_wrapped_budget_error_reads_the_same(self):
        error = budget_error(budget_exc())
        assert describe_llm_error(ProviderBudgetExceededError(error)) == describe_llm_error(
            budget_exc()
        )

    def test_plain_error_keeps_its_class_without_litellm_prefixes(self):
        text = describe_llm_error(_api_error("OpenrouterException - [SSL: UNEXPECTED_EOF]"))
        assert text == "APIError: [SSL: UNEXPECTED_EOF]"
        assert describe_llm_error(RuntimeError("boom")) == "RuntimeError: boom"

    def test_long_detail_is_cut_at_a_word_boundary(self):
        text = describe_llm_error(RuntimeError("word " * 200), max_chars=50)
        assert len(text) <= 50
        assert text.endswith("word…")


class TestClip:
    def test_short_text_is_untouched_but_whitespace_collapsed(self):
        assert clip("a  b\n c", 10) == "a b c"

    def test_without_a_nearby_space_cuts_hard(self):
        assert clip("x" * 20, 10) == "x" * 9 + "…"
