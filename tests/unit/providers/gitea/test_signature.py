"""``X-Gitea-Signature``: bare-hex HMAC-SHA256 of the raw body."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from issue_agent.providers.base import WebhookRejectedError
from issue_agent.providers.gitea.webhook import GiteaWebhookAdapter, verify_webhook_signature

SECRET = "s3cr3t"
PAYLOAD = json.dumps({"action": "opened", "issue": {"number": 1}}).encode()
FIXTURES = Path(__file__).parent / "fixtures"


def _sign(payload: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


class TestValidSignature:
    def test_typical_payload(self):
        assert verify_webhook_signature(PAYLOAD, _sign(PAYLOAD), SECRET)

    def test_empty_payload(self):
        assert verify_webhook_signature(b"", _sign(b""), SECRET)

    def test_uppercase_hex(self):
        assert verify_webhook_signature(PAYLOAD, _sign(PAYLOAD).upper(), SECRET)

    def test_real_capture_verifies_with_the_capture_secret(self):
        # capture.py's throwaway secret; the recorded header is Gitea's own.
        body = (FIXTURES / "issues_opened.json").read_bytes()
        headers = json.loads((FIXTURES / "headers" / "issues_opened.json").read_text())

        assert verify_webhook_signature(
            body, headers["X-Gitea-Signature"], "capture-webhook-secret"
        )


class TestInvalidSignature:
    def test_wrong_secret(self):
        assert not verify_webhook_signature(PAYLOAD, _sign(PAYLOAD), "wrong")

    def test_tampered_payload(self):
        assert not verify_webhook_signature(PAYLOAD + b" ", _sign(PAYLOAD), SECRET)

    @pytest.mark.parametrize("secret", ["", "   "])
    def test_empty_secret_fails_closed(self, secret: str):
        assert not verify_webhook_signature(PAYLOAD, _sign(PAYLOAD, secret), secret)

    def test_empty_signature_fails_closed(self):
        # What Gitea sends from a hook configured without a secret.
        assert not verify_webhook_signature(PAYLOAD, "", SECRET)

    def test_github_style_prefix_is_not_gitea_format(self):
        assert not verify_webhook_signature(PAYLOAD, f"sha256={_sign(PAYLOAD)}", SECRET)

    @pytest.mark.parametrize("signature", ["zz", "abc", "not hex at all"])
    def test_not_hex(self, signature: str):
        assert not verify_webhook_signature(PAYLOAD, signature, SECRET)

    def test_truncated_digest(self):
        assert not verify_webhook_signature(PAYLOAD, _sign(PAYLOAD)[:32], SECRET)


class TestAdapter:
    def _adapter(self, secret: str = SECRET) -> GiteaWebhookAdapter:
        return GiteaWebhookAdapter(instance_id="gitea", secret=secret, bot_login="issue-agent")

    def test_reads_x_gitea_signature(self):
        self._adapter().verify_signature({"X-Gitea-Signature": _sign(PAYLOAD)}, PAYLOAD)

    def test_rejection_never_marks_the_delivery(self):
        with pytest.raises(WebhookRejectedError) as exc:
            self._adapter().verify_signature({"X-Gitea-Signature": "00"}, PAYLOAD)

        assert exc.value.mark_reason is None
        assert exc.value.log_event == "invalid_signature"
        assert exc.value.status == 400

    def test_missing_header_is_rejected(self):
        with pytest.raises(WebhookRejectedError):
            self._adapter().verify_signature({}, PAYLOAD)

    def test_compat_headers_are_ignored(self):
        # Gitea also sends GitHub/Gogs-style headers; a valid one of those
        # must not stand in for X-Gitea-Signature.
        headers = {
            "X-Hub-Signature-256": f"sha256={_sign(PAYLOAD)}",
            "X-Gogs-Signature": _sign(PAYLOAD),
        }

        with pytest.raises(WebhookRejectedError):
            self._adapter().verify_signature(headers, PAYLOAD)

    def test_empty_configured_secret_rejects_a_matching_signature(self):
        with pytest.raises(WebhookRejectedError):
            self._adapter("").verify_signature({"X-Gitea-Signature": _sign(PAYLOAD, "")}, PAYLOAD)
