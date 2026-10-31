from __future__ import annotations

from unittest.mock import MagicMock, patch

from issue_agent.observability.alerts import send_budget_alert


def _make_mock_client() -> MagicMock:
    """Returns a mock httpx.Client whose ``__enter__`` returns itself."""
    client = MagicMock()
    client.__enter__.return_value = client
    return client


class TestSendBudgetAlertNoop:
    def test_empty_url_does_nothing(self) -> None:
        with patch("issue_agent.observability.alerts.httpx.Client") as mock_cls:
            send_budget_alert("", "daily_budget_exceeded", 60.0, 50.0)
        mock_cls.assert_not_called()

    def test_none_url_does_nothing(self) -> None:
        with patch("issue_agent.observability.alerts.httpx.Client") as mock_cls:
            send_budget_alert("", "daily_budget_exceeded", 60.0, 50.0)
        mock_cls.assert_not_called()


class TestSendBudgetAlertDiscord:
    DISCORD_URL = "https://discord.com/api/webhooks/123456/abcdef"

    def test_discord_payload_has_embeds(self) -> None:
        client = _make_mock_client()
        with patch("issue_agent.observability.alerts.httpx.Client", return_value=client):
            send_budget_alert(self.DISCORD_URL, "daily_budget_exceeded", 60.0, 50.0)

        client.post.assert_called_once()
        _call_kwargs = client.post.call_args
        url, payload = _call_kwargs[0][0], _call_kwargs[1]["json"]
        assert url == self.DISCORD_URL
        assert "content" in payload
        assert "embeds" in payload
        assert len(payload["embeds"]) == 1
        embed = payload["embeds"][0]
        assert embed["title"] == "Budget Exceeded"
        assert embed["color"] == 15158332
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["Event"] == "daily_budget_exceeded"
        assert fields["Spent"] == "$60.00"
        assert fields["Limit"] == "$50.00"

    def test_discord_alert_with_task_context(self) -> None:
        client = _make_mock_client()
        with patch("issue_agent.observability.alerts.httpx.Client", return_value=client):
            send_budget_alert(
                self.DISCORD_URL,
                "per_task_budget_exceeded",
                2.50,
                2.00,
                task_id="task-001",
                issue_number=42,
                repo="stateway-io/stateway-issues",
            )

        payload = client.post.call_args[1]["json"]
        assert "task-001" in payload["content"]
        assert "#42" in payload["content"]
        assert "stateway-io/stateway-issues" in payload["content"]


class TestSendBudgetAlertTelegram:
    TELEGRAM_URL = "https://api.telegram.org/bot123456:ABC-DEF/sendMessage?chat_id=-1001234567890"

    def test_telegram_payload_has_chat_id_and_text(self) -> None:
        client = _make_mock_client()
        with patch("issue_agent.observability.alerts.httpx.Client", return_value=client):
            send_budget_alert(self.TELEGRAM_URL, "daily_budget_exceeded", 60.0, 50.0)

        payload = client.post.call_args[1]["json"]
        assert payload["chat_id"] == "-1001234567890"
        assert "text" in payload
        assert payload["parse_mode"] == "Markdown"
        assert "Budget Alert" in payload["text"]

    def test_telegram_missing_chat_id_logs_warning(self) -> None:
        bad_url = "https://api.telegram.org/bot123456:ABC-DEF/sendMessage"
        client = _make_mock_client()
        with patch("issue_agent.observability.alerts.httpx.Client", return_value=client):
            send_budget_alert(bad_url, "daily_budget_exceeded", 60.0, 50.0)
        client.post.assert_not_called()


class TestSendBudgetAlertGeneric:
    GENERIC_URL = "https://hooks.example.com/alerts"

    def test_generic_payload_has_flat_fields(self) -> None:
        client = _make_mock_client()
        with patch("issue_agent.observability.alerts.httpx.Client", return_value=client):
            send_budget_alert(self.GENERIC_URL, "daily_budget_exceeded", 60.0, 50.0)

        payload = client.post.call_args[1]["json"]
        assert "content" in payload
        assert payload["event"] == "daily_budget_exceeded"
        assert payload["spent_usd"] == 60.0
        assert payload["limit_usd"] == 50.0


class TestSendBudgetAlertErrors:
    DISCORD_URL = "https://discord.com/api/webhooks/123456/abcdef"

    def test_http_error_logged_not_raised(self) -> None:
        client = _make_mock_client()
        client.post.side_effect = Exception("connection error")
        with patch("issue_agent.observability.alerts.httpx.Client", return_value=client):
            send_budget_alert(self.DISCORD_URL, "daily_budget_exceeded", 60.0, 50.0)
        client.post.assert_called_once()
