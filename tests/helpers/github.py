"""GitHub webhook payloads, signatures and a stubbed plugin for tests (any tier)."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from issue_agent.providers.github.auth import GitHubAppAuth
from issue_agent.providers.github.normalize import bot_identity_from_settings
from issue_agent.providers.github.plugin import GitHubProviderPlugin

if TYPE_CHECKING:
    from issue_agent.config import Settings

TEST_SECRET = "test-webhook-secret"
DEFAULT_INSTALLATION_ID = 12345
GITHUB_TERMS = GitHubProviderPlugin.capabilities.terms


def sign_payload(payload: bytes, secret: str = TEST_SECRET) -> str:
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def make_github_payload(
    event: str = "issues",
    action: str = "opened",
    repo: str = "stateway-io/stateway-issues",
    issue_number: int = 1,
    labels: list[str] | None = None,
    issue_state: str = "open",
    comment: dict | None = None,
    installation_id: int | None = DEFAULT_INSTALLATION_ID,
    issue_author: str = "test-user",
    is_pull_request: bool = False,
) -> bytes:
    payload = {
        "action": action,
        "issue": {
            "number": issue_number,
            "title": "Test issue",
            "body": "Test body",
            "state": issue_state,
            "labels": [{"name": lb} for lb in (labels or [])],
            "user": {"login": issue_author},
        },
        "repository": {"full_name": repo},
        "sender": {"login": "test-user"},
    }
    if is_pull_request:
        payload["issue"]["pull_request"] = {
            "url": "https://api.github.com/repos/owner/repo/pulls/1"
        }
    if comment is not None:
        payload["comment"] = comment
    # Present by default, as GitHub App deliveries always carry it. Pass
    # installation_id=None for a task the worker must resolve live.
    if installation_id is not None:
        payload["installation"] = {"id": installation_id}
    return json.dumps(payload).encode()


def stub_github_plugin(settings: Settings, token: str = "test-token") -> GitHubProviderPlugin:
    """The real GitHub plugin over a stubbed App auth (installation 1, a
    fixed token): no key, no network. Patch ``GitHubClient`` methods to
    fake the API itself."""
    auth = MagicMock(spec=GitHubAppAuth)
    auth.installation_for.return_value = 1
    auth.token_for.return_value = token
    config = settings.providers["github"]
    return GitHubProviderPlugin(
        "github",
        webhook_secret=config.webhook_secret,
        identity=bot_identity_from_settings(config, settings.agent_name),
        host=config.host,
        label_agent_generated=settings.pr_label_agent_generated,
        auth_factory=lambda: auth,
    )
