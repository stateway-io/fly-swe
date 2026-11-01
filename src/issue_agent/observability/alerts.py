from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from issue_agent.logging import get_logger

logger = get_logger(__name__)

_ALERT_TIMEOUT_SECONDS = 10
_DISCORD_RED = 15158332


def _detect_target(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if "discord.com" in host and "webhooks" in parsed.path:
        return "discord"
    if "api.telegram.org" in host and parsed.path.startswith("/bot"):
        return "telegram"
    return "generic"


def _format_metric(metric: str, spent: float, limit: float) -> str:
    """``spent``/``limit`` are dollars for ``metric="cost"`` (the historical
    default) or a raw iteration count for ``metric="iterations"`` — the
    label and formatting differ so an iteration-limit alert doesn't read as
    a dollar amount that was never actually reached."""
    if metric == "iterations":
        return f"Iterations used: {int(spent)} / {int(limit)}"
    return f"Spent: ${spent:.2f} / ${limit:.2f}"


def _build_message(
    event: str,
    spent_usd: float,
    limit_usd: float,
    *,
    metric: str = "cost",
    task_id: str | None = None,
    issue_number: int | None = None,
    repo: str | None = None,
) -> str:
    lines = [
        f"Budget Alert — {event}",
        _format_metric(metric, spent_usd, limit_usd),
    ]
    if task_id:
        lines.append(f"Task: {task_id}")
    if issue_number is not None:
        lines.append(f"Issue: #{issue_number}")
    if repo:
        lines.append(f"Repo: {repo}")
    return "\n".join(lines)


def _build_telegram_text(
    event: str,
    spent_usd: float,
    limit_usd: float,
    *,
    metric: str = "cost",
    task_id: str | None = None,
    issue_number: int | None = None,
    repo: str | None = None,
) -> str:
    lines = [
        f"🚨 *Budget Alert — {event}*",
        "",
        f"*{_format_metric(metric, spent_usd, limit_usd)}*",
    ]
    if task_id:
        lines.append(f"Task: `{task_id}`")
    if issue_number is not None:
        lines.append(f"Issue: #{issue_number}")
    if repo:
        lines.append(f"Repo: `{repo}`")
    return "\n".join(lines)


def send_budget_alert(
    webhook_url: str,
    event: str,
    spent_usd: float,
    limit_usd: float,
    *,
    metric: str = "cost",
    task_id: str | None = None,
    issue_number: int | None = None,
    repo: str | None = None,
) -> None:
    """``spent_usd``/``limit_usd`` hold dollars for the default
    ``metric="cost"``, or a raw iteration count when ``metric="iterations"``
    (the parameter names stay as-is for backward compatibility with existing
    callers/tests; only the formatting changes)."""
    if not webhook_url:
        return

    target = _detect_target(webhook_url)
    field_label = "Iterations used" if metric == "iterations" else "Spent"
    limit_label = "Iteration limit" if metric == "iterations" else "Limit"
    field_value = f"{int(spent_usd)}" if metric == "iterations" else f"${spent_usd:.2f}"
    limit_value = f"{int(limit_usd)}" if metric == "iterations" else f"${limit_usd:.2f}"

    if target == "discord":
        text = _build_message(
            event,
            spent_usd,
            limit_usd,
            metric=metric,
            task_id=task_id,
            issue_number=issue_number,
            repo=repo,
        )
        now = datetime.now(UTC)
        payload: dict[str, Any] = {
            "content": text,
            "embeds": [
                {
                    "title": "Budget Exceeded",
                    "color": _DISCORD_RED,
                    "fields": [
                        {"name": "Event", "value": event, "inline": True},
                        {"name": field_label, "value": field_value, "inline": True},
                        {"name": limit_label, "value": limit_value, "inline": True},
                    ],
                    "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S") + "Z",
                }
            ],
        }

    elif target == "telegram":
        text = _build_telegram_text(
            event,
            spent_usd,
            limit_usd,
            metric=metric,
            task_id=task_id,
            issue_number=issue_number,
            repo=repo,
        )
        parsed = urlparse(webhook_url)
        qs = parse_qs(parsed.query)
        chat_ids = qs.get("chat_id")
        if not chat_ids:
            logger.warning("telegram_alert_missing_chat_id", webhook_url=webhook_url)
            return
        chat_id = chat_ids[0]
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }

    else:
        text = _build_message(
            event,
            spent_usd,
            limit_usd,
            metric=metric,
            task_id=task_id,
            issue_number=issue_number,
            repo=repo,
        )
        payload = {
            "content": text,
            "event": event,
            "spent_usd": spent_usd,
            "limit_usd": limit_usd,
            "metric": metric,
        }

    try:
        with httpx.Client(timeout=_ALERT_TIMEOUT_SECONDS) as client:
            resp = client.post(webhook_url, json=payload)
            resp.raise_for_status()
    except httpx.HTTPError:
        logger.warning("budget_alert_webhook_failed", event_name=event, target=target)
    except Exception:
        logger.exception("budget_alert_unexpected_error", event_name=event)
