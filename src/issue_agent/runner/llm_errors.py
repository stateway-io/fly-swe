"""Make sense of the exceptions LLM providers raise through LiteLLM.

LiteLLM stringifies a provider failure as a prefix chain plus the raw response
body — e.g. ``litellm.APIError: APIError: OpenrouterException - {"error": …}`` —
and doesn't always map it to a specific class: an OpenRouter budget guardrail
(HTTP 403) arrives as a generic ``APIError``, which the retry layers would treat
as transient. This module extracts the provider's own message from that body,
recognises spending-limit failures so they abort instead of being retried, and
renders a clean, bounded line for GitHub comments.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# "litellm.APIError: APIError: OpenrouterException - " and its variants.
_LITELLM_PREFIX = re.compile(
    r"^(?:(?:litellm\.)?[A-Za-z]+(?:Error|Exception)(?::| -)\s*)+"
)
_BUDGET_MESSAGE = re.compile(
    r"budget limit exceeded|insufficient credits|key limit exceeded|"
    r"credit limit exceeded|spend(?:ing)? limit",
    re.IGNORECASE,
)
_RESETS_AT = re.compile(r"\bresets\s+(\d{4}-\d{2}-\d{2}(?:T[\d:.]+Z?)?)", re.IGNORECASE)
_SPENT_OF_LIMIT = re.compile(r"\$(\d+(?:\.\d+)?)\s+of\s+\$(\d+(?:\.\d+)?)\s+used", re.IGNORECASE)


@dataclass(frozen=True)
class ProviderError:
    """What the provider said, parsed out of LiteLLM's exception text."""

    message: str
    code: int | None = None
    limit_source: str = ""
    remedy_hint: str = ""
    resets_at: str = ""
    spent_usd: float | None = None
    limit_usd: float | None = None


class ProviderBudgetExceededError(RuntimeError):
    """The provider refused the call because a spending limit (org budget,
    key limit, prepaid credits) is exhausted. Retrying can't help until a
    human raises the limit or it resets, so this aborts the call at once."""

    def __init__(self, error: ProviderError) -> None:
        super().__init__(error.message)
        self.error = error


def clip(text: str, max_chars: int) -> str:
    """Collapse whitespace and cap at ``max_chars``, cutting at a word boundary
    (when one is reasonably close) and marking the cut with ``…``."""
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1]
    head, sep, _ = cut.rpartition(" ")
    if sep and len(head) >= max_chars // 2:
        cut = head
    return cut.rstrip(" ,;:-—") + "…"


def _strip_prefixes(text: str) -> str:
    return _LITELLM_PREFIX.sub("", text.strip()).strip()


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_provider_error(exc: BaseException) -> ProviderError | None:
    """The provider's error, or ``None`` when ``exc`` carries no message."""
    text = _strip_prefixes(str(exc))
    status = _as_int(getattr(exc, "status_code", None))
    message, code, limit_source, remedy_hint = text, status, "", ""

    start = text.find("{")
    if start != -1:
        try:
            body, _ = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and error.get("message"):
            message = str(error["message"])
            code = _as_int(error.get("code")) or status
            metadata = error.get("metadata")
            if isinstance(metadata, dict):
                limit_source = str(metadata.get("limit_source") or "")
                remedy_hint = str(metadata.get("remedy_hint") or "")

    message = " ".join(message.split())
    if not message:
        return None
    resets = _RESETS_AT.search(message)
    spent = _SPENT_OF_LIMIT.search(message)
    return ProviderError(
        message=message,
        code=code,
        limit_source=limit_source,
        remedy_hint=" ".join(remedy_hint.split()),
        resets_at=resets.group(1) if resets else "",
        spent_usd=float(spent.group(1)) if spent else None,
        limit_usd=float(spent.group(2)) if spent else None,
    )


def budget_error(exc: BaseException) -> ProviderError | None:
    """The parsed error when ``exc`` is a provider spending-limit refusal
    (402, a 403 from a budget guardrail, or a budget/credits message); a
    plain 403 auth/permission failure is not one."""
    if isinstance(exc, ProviderBudgetExceededError):
        return exc.error
    error = parse_provider_error(exc)
    if error is None:
        return None
    if error.code == 402:
        return error
    if error.code == 403 and "budget" in error.limit_source.lower():
        return error
    if _BUDGET_MESSAGE.search(error.message):
        return error
    return None


def describe_llm_error(exc: BaseException, max_chars: int = 300) -> str:
    """One clean line for a GitHub comment: the provider's own message (plus
    its remedy hint) when the body has one, else ``Class: message`` without
    LiteLLM's prefix chain — never a raw JSON dump cut mid-string."""
    if isinstance(exc, ProviderBudgetExceededError):
        error: ProviderError | None = exc.error
        from_body = True
    else:
        error = parse_provider_error(exc)
        from_body = error is not None and error.message != " ".join(
            _strip_prefixes(str(exc)).split()
        )
    if error is not None and from_body:
        text = error.message
        if error.remedy_hint:
            text += f" — {error.remedy_hint}"
    elif error is not None:
        text = f"{type(exc).__name__}: {error.message}"
    else:
        text = type(exc).__name__
    return clip(text, max_chars)
