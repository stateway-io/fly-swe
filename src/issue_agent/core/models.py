"""Provider-independent domain model (PRD §3.1).

The webhook adapter normalizes each forge's payload into these models once,
at ingestion; nothing downstream of the parser reads a forge's raw payload.
``repo`` is an opaque slug — core code never splits it on ``/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 — pydantic resolves it at runtime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class TriggerKind(StrEnum):
    ISSUE_OPENED = "issue_opened"
    LABEL_ADDED = "label_added"
    COMMENT_COMMAND = "comment_command"
    # R7.1/R7.2: issue lifecycle events. The handler acts on these
    # directly — closing or touching `issue_work` — and never enqueues a Task,
    # so no `tasks` row ever carries these values.
    ISSUE_CLOSED = "issue_closed"
    ISSUE_REOPENED = "issue_reopened"


# "unknown": the forge's webhook doesn't say. The comment filter then can't
# decide by association at ingestion and defers to the worker (§3.1).
Association = Literal["owner", "member", "collaborator", "contributor", "none", "bot", "unknown"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Author(_Frozen):
    login: str = ""
    association: Association = "unknown"
    # True when the account is this service's own bot.
    is_self: bool = False


class Comment(_Frozen):
    id: str
    author: Author
    body: str = ""
    created_at: datetime | None = None


class IssueRef(_Frozen):
    # Configured provider instance id ("github", "gitlab-corp", ...).
    provider_instance: str
    repo: str
    # The number visible in the forge's UI/API (GitHub number, GitLab iid).
    number: int
    # The forge's global id, when distinct from `number`.
    global_id: str | None = None
    title: str = ""
    body: str = ""
    # The issue's *current* label set — not a diff (§3.1).
    labels: list[str] = []
    author: Author = Author()
    url: str = ""
    # None when the forge's event doesn't say; only a known "closed" makes
    # the ingestion filter drop a comment.
    state: Literal["open", "closed"] | None = None


class PRRef(_Frozen):
    """An open pull/merge request on the target repo."""

    number: int
    # The forge's web page for it — what a comment links to.
    url: str
    # The branch it merges from.
    head_ref: str


class TriggerEvent(_Frozen):
    kind: TriggerKind
    # issue_comment-style events also fire on PRs/MRs.
    target_kind: Literal["issue", "pr"] = "issue"
    issue: IssueRef
    # Whoever caused the event (GitHub's `sender`): the opener, the
    # labeler or the commenter. Not necessarily the issue author.
    actor: Author
    # Only on comment_command; the history comes from the forge client.
    trigger_comment: Comment | None = None
    # Idempotency key computed by the adapter.
    delivery_key: str
    # Opaque; its meaning is the provider's (GitHub: installation id).
    credentials_ref: str | None = None


@dataclass(frozen=True)
class Ignored:
    """A well-formed delivery the agent deliberately doesn't act on.

    ``reason`` is what the webhook response reports. ``repo`` lets the
    handler keep reporting ``repo_not_allowed`` first for repos it doesn't
    route, whatever else is wrong with the event; ``None`` when the event
    names no repo at all (e.g. an unsupported event type).
    """

    reason: str
    repo: str | None
