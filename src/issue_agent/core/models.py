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
    # Issue lifecycle events. The handler acts on these
    # directly — closing or touching `issue_work` — and never enqueues a Task,
    # so no `tasks` row ever carries these values.
    ISSUE_CLOSED = "issue_closed"
    ISSUE_REOPENED = "issue_reopened"
    # Harness-internal: the CI monitor saw the forge's CI fail on a PR the
    # agent opened and enqueued a fix attempt. Never parsed from a webhook.
    CI_FAILED = "ci_failed"


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


class PRState(_Frozen):
    """Where a pull/merge request stands on the forge — what data
    retention reads to know a work item's history is done."""

    state: Literal["open", "merged", "closed"]
    # When it was merged (``merged``) or closed unmerged (``closed``), in
    # UTC; None while open. The retention clock starts here.
    closed_at: datetime | None = None


# The verdict on one commit's CI, folded across every check/status the forge
# reports: "pending" while anything still runs; once everything finished,
# "failure" if anything failed and "success" otherwise; "none" when the forge
# reports nothing at all (the repo has no CI, or it hasn't started yet).
CIState = Literal["pending", "success", "failure", "none"]


class CICheck(_Frozen):
    """One check/status the forge reports on a commit."""

    name: str
    # pending | success | failure | neutral | skipped — the forge's own
    # conclusion, folded into those five.
    conclusion: Literal["pending", "success", "failure", "neutral", "skipped"]
    # The forge's web page for it; "" when it has none.
    url: str = ""
    # Opaque to the core: what the provider needs to fetch its log.
    ref_id: str = ""
    # A one-line description, when the forge gives one.
    summary: str = ""


class CIStatus(_Frozen):
    state: CIState
    checks: list[CICheck] = []

    @property
    def failed(self) -> list[CICheck]:
        return [c for c in self.checks if c.conclusion == "failure"]


class CIFailureLog(_Frozen):
    """What a failed check left behind — untrusted text produced by the
    target repo's own code, handed to the model only as data."""

    check_name: str
    url: str = ""
    excerpt: str = ""


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
