"""Ports a provider plugin implements (PRD §3.2).

Inbound: ``WebhookAdapter`` turns a delivery into a canonical event.
Outbound: ``ForgeClient`` (issues and comments), the optional
``PullRequestClient`` (absent on a forge without a PR API, §3.7) and
``GitTransport`` (clone/push auth and the forge's URL and reference syntax).
The clients are bound to one repo and resolve that repo's credentials
themselves, so the core never handles a token or an installation id.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Mapping

    from issue_agent.config import Settings
    from issue_agent.core.models import (
        Association,
        Author,
        Comment,
        Ignored,
        PRRef,
        TriggerEvent,
    )


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ProviderSettingsBase(BaseModel):
    """One instance's entry under ``providers.<id>``. ``type`` is the
    discriminator; ``secret_fields`` are read from env only and rejected in
    config.yaml."""

    model_config = ConfigDict(extra="forbid")

    secret_fields: ClassVar[frozenset[str]] = frozenset()

    type: str


class ProviderTerms(_Frozen):
    """The forge's vocabulary, for text the core writes (comments, PRs)."""

    issue: str
    pr: str
    ref_prefix_issue: str
    ref_prefix_pr: str
    repo_instructions_files: list[str] = []


class ProviderCapabilities(_Frozen):
    draft_mode: Literal["native", "title_prefix", "none"]
    # "Draft: " (GitLab), "WIP: " (Gitea/Forgejo); only with title_prefix.
    draft_prefix: str | None = None
    # False (Gogs): push + a comment with a compare link instead of a PR.
    supports_pr_api: bool
    supports_labels: bool
    # None: the forge may not auto-close, so the PR text must not promise it.
    auto_close_keyword: str | None
    # Bodies above this are truncated by the core.
    max_body_length: int = Field(gt=0)
    terms: ProviderTerms


class WebhookRejectedError(Exception):
    """A delivery the dispatcher must answer with an error.

    ``error`` is the response body's ``error``; ``log_event`` says why in the
    log, since the access line alone doesn't. ``mark_reason`` records the
    delivery as processed with that reason — ``None`` leaves it unrecorded,
    which an unauthenticated request must never be able to change.
    """

    def __init__(
        self,
        error: str,
        *,
        log_event: str,
        mark_reason: str | None = None,
        status: int = 400,
    ) -> None:
        super().__init__(error)
        self.error = error
        self.log_event = log_event
        self.mark_reason = mark_reason
        self.status = status


@dataclass(frozen=True)
class ParsedDelivery:
    """What a verified delivery turned into.

    ``event``/``action`` are the forge's own names, for the log and the
    ``processed_deliveries`` row; the core never branches on them.
    """

    event: str
    action: str | None
    result: TriggerEvent | Ignored


class WebhookAdapter(ABC):
    """Turns a forge's webhook delivery into a canonical event.

    The dispatcher calls, in order: ``delivery_key``, ``verify_signature``,
    then — unless the key was already processed — ``parse_event``. Each may
    raise ``WebhookRejectedError``. ``parse_event`` only normalizes; whether the
    event is worth a task is the core's ``policy/ingestion.py``.
    """

    @abstractmethod
    def delivery_key(self, headers: Mapping[str, str], body: bytes) -> str:
        """The idempotency key, stable across redeliveries of one event."""

    @abstractmethod
    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> None:
        """Raise ``WebhookRejectedError`` unless the delivery is authentic.

        Fails closed: with no secret configured, nothing is authentic.
        """

    @abstractmethod
    def event_name(self, headers: Mapping[str, str]) -> str:
        """The forge's event name, for logs."""

    @abstractmethod
    def parse_event(self, headers: Mapping[str, str], body: bytes) -> ParsedDelivery:
        """Normalize a verified delivery. ``Ignored`` for events the agent
        doesn't handle; ``WebhookRejectedError`` for a malformed body."""


class ProviderHealth(_Frozen):
    """A readiness verdict. ``checks`` maps a check's name to "ok", a
    failure, or an informational "warning: ..." that doesn't affect ``ok``."""

    ok: bool
    checks: dict[str, str] = {}


class ForgeClient(ABC):
    """Issue operations on one repo. Credentials are resolved (and renewed)
    inside; every method may raise on a network or API error."""

    @abstractmethod
    def get_issue_state(self, number: int) -> Literal["open", "closed"]: ...

    @abstractmethod
    def list_comments(self, number: int, limit: int) -> list[Comment]:
        """The issue's ``limit`` most recent comments, oldest first, with
        ``author.is_self`` set on this service's own."""

    @abstractmethod
    def post_comment(self, number: int, body: str) -> str:
        """Comment on the issue; returns the comment's URL."""

    @abstractmethod
    def get_default_branch(self) -> str: ...

    @abstractmethod
    def add_labels(self, number: int, labels: list[str]) -> None: ...

    def author_permission(self, login: str) -> Association:
        """``login``'s standing on the repo, for a comment whose webhook
        didn't say (``association == "unknown"``, §3.1). The default never
        grants anything."""
        return "unknown"


class PullRequestClient(ABC):
    """PR operations on one target repo. Only a forge with a PR API has one
    (``capabilities.supports_pr_api``); without it the core publishes the
    branch instead (§3.7)."""

    @abstractmethod
    def find_open_pr(self, branch: str) -> PRRef | None:
        """The open PR whose head is exactly ``branch``, or None."""

    @abstractmethod
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
        """Open a PR and label it. The core asks for ``draft`` and the
        plugin applies it as ``capabilities.draft_mode`` allows."""

    @abstractmethod
    def comment_on_pr(self, pr: PRRef, body: str) -> str:
        """Comment on the PR's conversation; returns the comment's URL."""

    @abstractmethod
    def pr_has_conflicts(self, pr: PRRef, head_sha: str) -> bool | None:
        """Whether the forge reports ``pr`` as conflicting with its base once
        it has caught up with ``head_sha`` (the commit just pushed). None
        when it can't tell — the caller only acts on a definite True."""


class GitTransport(ABC):
    """How git reaches the forge, and how its web UI is addressed."""

    @abstractmethod
    def clone_url(self, repo: str) -> str:
        """The remote URL — never carrying a credential."""

    @abstractmethod
    def git_auth_env(self, repo: str) -> dict[str, str]:
        """Env vars that authenticate one git process against ``repo`` with a
        fresh credential, without writing it anywhere the agent can read."""

    @abstractmethod
    def pr_url(self, repo: str, number: int) -> str: ...

    @abstractmethod
    def compare_url(self, repo: str, base: str, head: str) -> str:
        """A page showing ``head`` against ``base`` — where a human opens the
        PR by hand on a forge without a PR API."""

    @abstractmethod
    def format_ref(
        self, kind: Literal["issue", "pr"], number: int, *, repo: str, from_repo: str
    ) -> str:
        """A reference to ``repo``'s issue/PR ``number`` as written in text
        that lives in ``from_repo`` (``#N``, ``!N``, ``owner/repo#N``)."""

    @abstractmethod
    def closing_reference(self, issue_repo: str, number: int, target_repo: str) -> str | None:
        """The PR-body line that closes the issue on merge (``Closes #N``),
        or None when the forge can't be trusted to."""

    def default_commit_email(self, name: str) -> str:
        """The commit email used when ``git_identity.user_email`` is unset."""
        return f"{name}@users.noreply.invalid"


class ProviderPlugin(ABC):
    """One configured instance of a forge type (PRD §3.3)."""

    type: ClassVar[str]
    capabilities: ClassVar[ProviderCapabilities]

    instance_id: str
    webhook: WebhookAdapter

    @classmethod
    @abstractmethod
    def from_settings(
        cls, instance_id: str, config: ProviderSettingsBase, settings: Settings
    ) -> Self:
        """Build the instance from its typed ``providers.<id>`` entry.
        ``settings`` is the root config, for what is the agent's rather
        than the provider's (its name, the PR label)."""

    @abstractmethod
    def bot_identity(self) -> Author:
        """This service's own account on the forge (``is_self=True``)."""

    @abstractmethod
    def client(self, repo: str) -> ForgeClient: ...

    def pulls(self, repo: str) -> PullRequestClient | None:
        """Override exactly when ``capabilities.supports_pr_api`` — the
        registry rejects a plugin where the two disagree."""
        return None

    @abstractmethod
    def transport(self) -> GitTransport: ...

    @abstractmethod
    def health_check(self) -> ProviderHealth:
        """Config-only readiness; must not raise or call the network."""
