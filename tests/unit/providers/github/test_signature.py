from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from issue_agent.providers.github.webhook import verify_webhook_signature


def _valid_header(payload: bytes, secret: str = "s3cr3t") -> str:
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


VALID_PAYLOAD = json.dumps({"action": "opened", "issue": {"number": 1}}).encode()
VALID_HEADER = _valid_header(VALID_PAYLOAD)


class TestValidSignature:
    def test_typical_webhook_payload(self):
        assert verify_webhook_signature(VALID_PAYLOAD, VALID_HEADER, "s3cr3t")

    def test_empty_payload(self):
        header = _valid_header(b"", "s3cr3t")
        assert verify_webhook_signature(b"", header, "s3cr3t")

    def test_lowercase_hex_in_header(self):
        header = f"sha256={_valid_header(VALID_PAYLOAD, 's3cr3t').removeprefix('sha256=').lower()}"
        assert verify_webhook_signature(VALID_PAYLOAD, header, "s3cr3t")


class TestInvalidSignature:
    def test_wrong_hmac(self):
        assert not verify_webhook_signature(VALID_PAYLOAD, VALID_HEADER, "wrong-secret")

    def test_tampered_payload(self):
        tampered = json.dumps({"action": "deleted"}).encode()
        assert not verify_webhook_signature(tampered, VALID_HEADER, "s3cr3t")

    def test_truncated_header_digest(self):
        header = "sha256=abc123"
        assert not verify_webhook_signature(VALID_PAYLOAD, header, "s3cr3t")


class TestMalformedHeader:
    def test_missing_prefix(self):
        header = _valid_header(VALID_PAYLOAD, "s3cr3t").removeprefix("sha256=")
        assert not verify_webhook_signature(VALID_PAYLOAD, header, "s3cr3t")

    def test_empty_header(self):
        assert not verify_webhook_signature(VALID_PAYLOAD, "", "s3cr3t")

    def test_whitespace_only_header(self):
        assert not verify_webhook_signature(VALID_PAYLOAD, "   ", "s3cr3t")

    def test_random_string_header(self):
        assert not verify_webhook_signature(VALID_PAYLOAD, "not-a-valid-header", "s3cr3t")

    def test_header_with_just_prefix(self):
        assert not verify_webhook_signature(VALID_PAYLOAD, "sha256=", "s3cr3t")

    def test_non_hex_chars(self):
        assert not verify_webhook_signature(VALID_PAYLOAD, "sha256=zzzzzzzzzz", "s3cr3t")


class TestTimingSafeComparison:
    def test_uses_compare_digest(self):
        assert verify_webhook_signature(VALID_PAYLOAD, VALID_HEADER, "s3cr3t")


class TestEdgeCases:
    def test_unicode_payload(self):
        payload = '{"msg": "café — 中文"}'.encode()
        header = _valid_header(payload, "s3cr3t")
        assert verify_webhook_signature(payload, header, "s3cr3t")

    def test_large_payload(self):
        payload = json.dumps({"data": "x" * 100_000}).encode()
        header = _valid_header(payload, "s3cr3t")
        assert verify_webhook_signature(payload, header, "s3cr3t")

    def test_secret_with_special_chars(self):
        secret = "hunter2!@#$%^&*()_+-=[]{}|;':\",./<>?"
        payload = b"test"
        header = _valid_header(payload, secret)
        assert verify_webhook_signature(payload, header, secret)
        assert not verify_webhook_signature(payload, header, "wrong")


class TestEmptySecretFailsClosed:
    @pytest.mark.parametrize("secret", ["", "   "])
    def test_signature_made_with_the_empty_secret_is_rejected(self, secret):
        # An HMAC keyed with "" is forgeable by anyone who knows the secret is
        # unset, so a deploy without one accepts nothing.
        header = _valid_header(VALID_PAYLOAD, secret)
        assert not verify_webhook_signature(VALID_PAYLOAD, header, secret)
