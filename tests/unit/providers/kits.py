"""Per-provider fixture kits for the contract suite (PRD §5).

A kit knows how to build its plugin — with its API credentials stubbed, so
the outbound ports work without a network — and how to produce one signed
delivery of each sample event in that forge's own wire format. Adding a provider type
to ``BUILTIN_PROVIDER_TYPES`` means adding a kit here; the contract suite
fails until one exists.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from unittest.mock import MagicMock

from issue_agent.providers.github.auth import GitHubAppAuth
from issue_agent.providers.github.normalize import BotIdentity
from issue_agent.providers.github.plugin import GitHubProviderPlugin
from tests.fakes.fake_provider import (
    FakeProvider,
    FakeProviderWithPulls,
    fake_body,
    fake_headers,
)

if TYPE_CHECKING:
    from issue_agent.providers.base import ProviderPlugin

# Every kit produces these, in its forge's format.
SAMPLES = (
    "issue_opened",
    "comment",
    "comment_on_pr",
    "own_bot_comment",
    "labels_changed_without_trigger",
    "unsupported",
)


@dataclass(frozen=True)
class Delivery:
    headers: dict[str, str]
    body: bytes


class ProviderKit(Protocol):
    type: str
    # False: authors always come out association="unknown".
    reports_association: bool

    def make(self, instance_id: str, secret: str) -> ProviderPlugin: ...

    def delivery(
        self, sample: str, *, secret: str, delivery_key: str | None = "delivery-1"
    ) -> Delivery: ...


_GITHUB_FIXTURES = Path(__file__).parent / "github" / "fixtures"
_GITHUB_SAMPLES = {
    "issue_opened": ("issues", "issues_opened"),
    "comment": ("issue_comment", "issue_comment_created"),
    "comment_on_pr": ("issue_comment", "issue_comment_on_pull_request"),
    "own_bot_comment": ("issue_comment", "issue_comment_own_bot"),
    "labels_changed_without_trigger": ("issues", "issues_labeled_without_trigger"),
    "unsupported": ("push", "issues_opened"),
}


class GitHubKit:
    type = "github"
    reports_association = True

    def make(self, instance_id: str, secret: str) -> ProviderPlugin:
        auth = MagicMock(spec=GitHubAppAuth)
        auth.installation_for.return_value = 1
        auth.token_for.return_value = "ghs_kit_token"
        return GitHubProviderPlugin(
            instance_id,
            webhook_secret=secret,
            identity=BotIdentity(app_id=3735121, app_name="Stateway Issue Agent"),
            auth_factory=lambda: auth,
        )

    def delivery(
        self, sample: str, *, secret: str, delivery_key: str | None = "delivery-1"
    ) -> Delivery:
        event, fixture = _GITHUB_SAMPLES[sample]
        body = (_GITHUB_FIXTURES / f"{fixture}.json").read_bytes()
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers = {"X-GitHub-Event": event, "X-Hub-Signature-256": f"sha256={digest}"}
        if delivery_key is not None:
            headers["X-GitHub-Delivery"] = delivery_key
        return Delivery(headers, body)


_COMMENT = {"id": 9001, "author": {"login": "maintainer", "association": "member"}, "body": "please fix"}
_FAKE_SAMPLES = {
    "issue_opened": ("issue", fake_body()),
    "comment": ("comment", fake_body("comment_command", comment=_COMMENT)),
    "comment_on_pr": ("comment", fake_body("comment_command", comment=_COMMENT, on_pr=True)),
    "own_bot_comment": (
        "comment",
        fake_body("comment_command", comment={"id": 9002, "author": {"login": "fake-agent"}}),
    ),
    "labels_changed_without_trigger": ("issue", fake_body("label_added", labels=["wontfix"])),
    "unsupported": ("wiki", fake_body()),
}


@dataclass(frozen=True)
class FakeKit:
    reports_association: bool = True
    with_pulls: bool = False
    type: str = "fake"

    def make(self, instance_id: str, secret: str) -> ProviderPlugin:
        cls = FakeProviderWithPulls if self.with_pulls else FakeProvider
        return cls(instance_id, secret=secret, reports_association=self.reports_association)

    def delivery(
        self, sample: str, *, secret: str, delivery_key: str | None = "delivery-1"
    ) -> Delivery:
        event, data = _FAKE_SAMPLES[sample]
        body = json.dumps(data).encode()
        return Delivery(
            fake_headers(body, secret=secret, event=event, delivery=delivery_key), body
        )


KITS: dict[str, ProviderKit] = {
    "github": GitHubKit(),
    "fake": FakeKit(),
    "fake-no-association": FakeKit(reports_association=False),
    "fake-with-prs": FakeKit(with_pulls=True),
}
