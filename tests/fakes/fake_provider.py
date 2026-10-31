"""An in-memory forge for testing the core against the provider ports
(PRD §5).

Its wire format is deliberately unlike GitHub's — its own headers, its own
JSON shape — so a core that still leaned on GitHub would fail against it.
Two variants matter: ``reports_association=False`` is a forge whose webhook
never says how an author relates to the repo, so every ``Author`` comes out
``unknown`` and the comment-author decision falls to the worker (§3.1).

Outbound, every client reads and writes one ``FakeForge``, so a test seeds
issues and inspects what the core posted. ``FakeProvider`` has no PR API —
the core must publish the branch instead (§3.7); ``FakeProviderWithPulls``
has one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import ValidationError

from issue_agent.core.models import (
    Author,
    Comment,
    Ignored,
    IssueRef,
    PRRef,
    TriggerEvent,
    TriggerKind,
)
from issue_agent.providers.base import (
    ForgeClient,
    GitTransport,
    ParsedDelivery,
    ProviderCapabilities,
    ProviderHealth,
    ProviderPlugin,
    ProviderTerms,
    PullRequestClient,
    WebhookAdapter,
    WebhookRejectedError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from issue_agent.config import Settings
    from issue_agent.core.models import Association
    from issue_agent.providers.base import ProviderSettingsBase

DELIVERY_HEADER = "X-Fake-Delivery"
EVENT_HEADER = "X-Fake-Event"
SIGNATURE_HEADER = "X-Fake-Signature"
FAKE_BOT_LOGIN = "fake-agent"
FAKE_HOST = "https://forge.example"

_EVENTS = {"issue", "comment"}


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class FakeWebhookAdapter(WebhookAdapter):
    def __init__(
        self, *, instance_id: str, secret: str, reports_association: bool
    ) -> None:
        self._instance_id = instance_id
        self._secret = secret
        self._reports_association = reports_association

    def delivery_key(self, headers: Mapping[str, str], body: bytes) -> str:
        key = (headers.get(DELIVERY_HEADER) or "").strip()
        if not key:
            raise WebhookRejectedError(
                "missing delivery id", log_event="missing_delivery_header"
            )
        return key

    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> None:
        given = (headers.get(SIGNATURE_HEADER) or "").strip()
        if not self._secret or not hmac.compare_digest(given, sign(body, self._secret)):
            raise WebhookRejectedError("invalid signature", log_event="invalid_signature")

    def event_name(self, headers: Mapping[str, str]) -> str:
        return (headers.get(EVENT_HEADER) or "").strip()

    def parse_event(self, headers: Mapping[str, str], body: bytes) -> ParsedDelivery:
        event = self.event_name(headers)
        if event not in _EVENTS:
            return ParsedDelivery(
                event, "unsupported_event", Ignored("unsupported_event", None)
            )
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError(data)
            result = self._normalize(self.delivery_key(headers, body), data)
        except (ValueError, ValidationError):
            raise WebhookRejectedError(
                "invalid payload", log_event="invalid_payload", mark_reason="invalid_payload"
            ) from None
        return ParsedDelivery(event, data.get("kind"), result)

    def _author(self, raw: dict[str, Any] | None) -> Author:
        raw = raw or {}
        login = raw.get("login", "")
        return Author(
            login=login,
            association=raw.get("association", "none")
            if self._reports_association
            else "unknown",
            is_self=login == FAKE_BOT_LOGIN,
        )

    def _normalize(self, key: str, data: dict[str, Any]) -> TriggerEvent | Ignored:
        repo = data["repo"]
        try:
            kind = TriggerKind(data["kind"])
        except ValueError:
            return Ignored("unhandled_event_action", repo)
        comment = data.get("comment")
        return TriggerEvent(
            kind=kind,
            target_kind="pr" if data.get("on_pr") else "issue",
            issue=IssueRef(
                provider_instance=self._instance_id,
                repo=repo,
                number=data["number"],
                title=data.get("title", ""),
                body=data.get("body", ""),
                labels=data.get("labels", []),
                author=self._author(data.get("author")),
                state=data.get("state"),
            ),
            actor=self._author(data.get("actor") or data.get("author")),
            trigger_comment=Comment(
                id=str(comment["id"]),
                author=self._author(comment.get("author")),
                body=comment.get("body", ""),
            )
            if comment
            else None,
            delivery_key=key,
            # No credentials hint: the worker resolves them (PRD Phase 2a).
            credentials_ref=None,
        )


@dataclass
class FakeForge:
    """The fake forge's whole state, shared by every client of a provider."""

    issue_states: dict[tuple[str, int], Literal["open", "closed"]] = field(
        default_factory=dict
    )
    comments: dict[tuple[str, int], list[Comment]] = field(default_factory=dict)
    labels: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    default_branches: dict[str, str] = field(default_factory=dict)
    permissions: dict[str, Association] = field(default_factory=dict)
    prs: dict[str, list[PRRef]] = field(default_factory=dict)
    # (repo, PR number) -> the title/body/base/draft it was opened with.
    pr_details: dict[tuple[str, int], dict[str, object]] = field(default_factory=dict)
    # PR number -> what `pr_has_conflicts` reports.
    conflicts: dict[int, bool | None] = field(default_factory=dict)
    # Every token handed to git, in order — each call mints a fresh one.
    tokens: list[str] = field(default_factory=list)

    def posted(self, repo: str, number: int) -> list[str]:
        """Bodies of the comments on ``repo``'s issue/PR ``number``."""
        return [c.body for c in self.comments.get((repo, number), [])]


class FakeForgeClient(ForgeClient):
    def __init__(self, forge: FakeForge, repo: str) -> None:
        self._forge = forge
        self._repo = repo

    def get_issue_state(self, number: int) -> Literal["open", "closed"]:
        return self._forge.issue_states.get((self._repo, number), "open")

    def list_comments(self, number: int, limit: int) -> list[Comment]:
        comments = self._forge.comments.get((self._repo, number), [])
        return comments[-limit:] if limit > 0 else []

    def post_comment(self, number: int, body: str) -> str:
        thread = self._forge.comments.setdefault((self._repo, number), [])
        comment_id = f"{self._repo}#{number}/{len(thread) + 1}"
        thread.append(
            Comment(
                id=comment_id,
                author=Author(login=FAKE_BOT_LOGIN, association="bot", is_self=True),
                body=body,
            )
        )
        return f"{FAKE_HOST}/{self._repo}/-/issues/{number}#{comment_id}"

    def get_default_branch(self) -> str:
        return self._forge.default_branches.get(self._repo, "main")

    def add_labels(self, number: int, labels: list[str]) -> None:
        self._forge.labels.setdefault((self._repo, number), []).extend(labels)

    def author_permission(self, login: str) -> Association:
        return self._forge.permissions.get(login, "unknown")


class FakePullRequestClient(PullRequestClient):
    def __init__(self, forge: FakeForge, repo: str) -> None:
        self._forge = forge
        self._repo = repo

    def find_open_pr(self, branch: str) -> PRRef | None:
        return next(
            (pr for pr in self._forge.prs.get(self._repo, []) if pr.head_ref == branch),
            None,
        )

    def create_pr(
        self,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool,
        labels: list[str],
    ) -> PRRef:
        prs = self._forge.prs.setdefault(self._repo, [])
        number = 100 + len(prs)
        pr = PRRef(
            number=number,
            url=f"{FAKE_HOST}/{self._repo}/-/merge_requests/{number}",
            head_ref=head,
        )
        prs.append(pr)
        self._forge.labels[(self._repo, number)] = list(labels)
        self._forge.pr_details[(self._repo, number)] = {
            "title": title, "body": body, "base": base, "draft": draft,
        }
        return pr

    def comment_on_pr(self, pr: PRRef, body: str) -> str:
        return FakeForgeClient(self._forge, self._repo).post_comment(pr.number, body)

    def pr_has_conflicts(self, pr: PRRef, head_sha: str) -> bool | None:
        return self._forge.conflicts.get(pr.number)


class FakeTransport(GitTransport):
    """GitLab-like syntax, so nothing GitHub-shaped passes by accident."""

    def __init__(self, forge: FakeForge) -> None:
        self._forge = forge

    def clone_url(self, repo: str) -> str:
        return f"{FAKE_HOST}/{repo}.git"

    def git_auth_env(self, repo: str) -> dict[str, str]:
        token = f"fake-token-{len(self._forge.tokens) + 1}"
        self._forge.tokens.append(token)
        credential = base64.b64encode(f"oauth2:{token}".encode()).decode()
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{FAKE_HOST}.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}",
            "GIT_TERMINAL_PROMPT": "0",
        }

    def pr_url(self, repo: str, number: int) -> str:
        return f"{FAKE_HOST}/{repo}/-/merge_requests/{number}"

    def compare_url(self, repo: str, base: str, head: str) -> str:
        return f"{FAKE_HOST}/{repo}/-/compare/{base}...{head}"

    def format_ref(
        self, kind: Literal["issue", "pr"], number: int, *, repo: str, from_repo: str
    ) -> str:
        ref = f"{'!' if kind == 'pr' else '#'}{number}"
        return ref if repo == from_repo else f"{repo}{ref}"

    def closing_reference(
        self, issue_repo: str, number: int, target_repo: str
    ) -> str | None:
        return None


class FakeProvider(ProviderPlugin):
    type = "fake"
    # The opposite of GitHub wherever a capability can differ.
    capabilities = ProviderCapabilities(
        draft_mode="title_prefix",
        draft_prefix="WIP: ",
        supports_pr_api=False,
        supports_labels=True,
        auto_close_keyword=None,
        max_body_length=1_000,
        terms=ProviderTerms(
            issue="ticket", pr="Merge Request", ref_prefix_issue="#", ref_prefix_pr="!"
        ),
    )

    def __init__(
        self,
        instance_id: str,
        *,
        secret: str,
        reports_association: bool = True,
        forge: FakeForge | None = None,
        health: ProviderHealth | None = None,
    ) -> None:
        self.instance_id = instance_id
        self.forge = forge or FakeForge()
        self._health = health or ProviderHealth(ok=True, checks={"config": "ok"})
        self.webhook = FakeWebhookAdapter(
            instance_id=instance_id,
            secret=secret,
            reports_association=reports_association,
        )

    @classmethod
    def from_settings(
        cls, instance_id: str, config: ProviderSettingsBase, settings: Settings
    ) -> Self:
        return cls(instance_id, secret=getattr(config, "webhook_secret", ""))

    def bot_identity(self) -> Author:
        return Author(login=FAKE_BOT_LOGIN, association="bot", is_self=True)

    def client(self, repo: str) -> FakeForgeClient:
        return FakeForgeClient(self.forge, repo)

    def transport(self) -> FakeTransport:
        return FakeTransport(self.forge)

    def health_check(self) -> ProviderHealth:
        return self._health


class FakeProviderWithPulls(FakeProvider):
    """The fake forge with a PR API (merge requests)."""

    capabilities = FakeProvider.capabilities.model_copy(update={"supports_pr_api": True})

    def pulls(self, repo: str) -> FakePullRequestClient:
        return FakePullRequestClient(self.forge, repo)


def fake_body(kind: str = "issue_opened", **fields: Any) -> dict[str, Any]:
    """A fake-forge payload; ``fields`` override the defaults."""
    body: dict[str, Any] = {
        "kind": kind,
        "repo": "stateway-io/stateway-issues",
        "number": 7,
        "title": "Broken link",
        "body": "The README has a broken link.",
        "labels": ["bug"],
        "state": "open",
        "author": {"login": "reporter", "association": "none"},
    }
    body.update(fields)
    return body


def fake_headers(
    body: bytes, *, secret: str, event: str = "issue", delivery: str | None = "fake-1"
) -> dict[str, str]:
    headers = {EVENT_HEADER: event, SIGNATURE_HEADER: sign(body, secret)}
    if delivery is not None:
        headers[DELIVERY_HEADER] = delivery
    return headers
