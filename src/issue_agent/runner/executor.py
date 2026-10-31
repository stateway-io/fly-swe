from __future__ import annotations

import contextlib
import os
import re
import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from issue_agent.core import pr_text
from issue_agent.logging import bind_context, get_logger
from issue_agent.observability.alerts import send_budget_alert
from issue_agent.observability.audit import write_audit_entry
from issue_agent.observability.prometheus_metrics import (
    observe_issue_work_closed,
    observe_resume,
    observe_task_result,
)
from issue_agent.policy.guards import check_daily_budget
from issue_agent.queue.models import IssueWork, Task, TaskStatus, TriggerKind
from issue_agent.runner.deps import install_project_dependencies
from issue_agent.runner.environment import _run as _run_sandboxed
from issue_agent.runner.environment import _sandboxed_env
from issue_agent.runner.guards import node_guard_env, resolve_node_heap_mb
from issue_agent.runner.llm_errors import (
    ProviderBudgetExceededError,
    ProviderError,
    budget_error,
    clip,
    describe_llm_error,
)
from issue_agent.runner.prompts import build_branch_status
from issue_agent.runner.toolchains import ToolchainManager
from issue_agent.runner.workspace import Workspace

if TYPE_CHECKING:
    from collections.abc import Callable

    from issue_agent.config import ProjectProfile, RepoRoute, Settings
    from issue_agent.core.models import Comment, PRRef
    from issue_agent.providers.base import (
        ForgeClient,
        GitTransport,
        ProviderCapabilities,
        ProviderPlugin,
        ProviderTerms,
        PullRequestClient,
    )
    from issue_agent.queue.store import SQLiteStore

logger = get_logger(__name__)


@dataclass
class AgentResult:
    success: bool
    cost_usd: float = 0.0
    iterations: int = 0
    error: str | None = None
    files_modified: list[str] = field(default_factory=list)
    # The LLM agent's own final report, written alongside the
    # COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT signal (mini-swe-agent's
    # `agent.run()` returns it under the "submission" key of the exit
    # message's `extra` dict). None when the harness raised before any exit
    # message was produced. run_task() uses this — falling back to the issue
    # title when it's missing/blank — as the PR body's description, so the
    # "summarize what you changed and why" instruction in prompts/system.md
    # actually feeds something instead of being silently discarded.
    submission: str | None = None
    # R5: on a block outcome (task_timeout / budget_exceeded / cost_unavailable)
    # the runner summarizes the interrupted attempt here — mechanical (zero
    # cost) or LLM per `resume.summary_on_block`. The executor persists it on
    # `issue_work` and injects it into the next run. None on success or when
    # resume is disabled; `block_summary_kind` is '' | 'mechanical' | 'llm'.
    block_summary: str | None = None
    block_summary_kind: str = ""
    # Human-readable detail for failures classified as `llm_error` /
    # `agent_crashed` / `provider_budget_exceeded` (the provider's own
    # message, or exception class + message; truncated) — shown in the abort
    # comment. `error` stays a stable code.
    error_detail: str | None = None
    # The parsed refusal behind `provider_budget_exceeded` — spent/limit for
    # the alert, the reset date for the comment.
    provider_error: ProviderError | None = None


class AgentRunner(Protocol):
    def run(
        self,
        workspace_path: str,
        task: Task,
        settings: Settings,
        route: RepoRoute,
        resume_summary: str | None = None,
        on_progress: Callable[[int, float], None] | None = None,
        system_template: str | None = None,
        toolchain_env: dict[str, str] | None = None,
        branch_status: str | None = None,
        comment_history: list[Comment] | None = None,
        *,
        terms: ProviderTerms,
    ) -> AgentResult: ...


_EXIT_STATUS_ERROR: dict[str, str] = {
    "TimeExceeded": "task_timeout",
    # LimitsExceeded is handled separately (_limits_exceeded_error) so the
    # error code reflects which limit actually fired.
    # max_consecutive_format_errors (3) responses the parser couldn't read in a
    # row. Deliberately NOT in _BLOCK_ERRORS: a block preserves WIP and
    # summarizes for a resume, which is a separate behavioral decision.
    "RepeatedFormatError": "format_error",
}


def _limits_exceeded_error(agent) -> str:
    """Which of mini-swe-agent's two ``LimitsExceeded`` triggers actually
    fired (it raises the same exception for both, see
    ``minisweagent.agents.default.DefaultAgent.step``, and never says which).

    Comparing the agent's own counters against its configured limits after
    the fact tells them apart, so the abort comment doesn't blame "budget"
    ($) for what was really the iteration/step limit — a distinction that
    matters to whoever reads the GitHub comment deciding whether to raise a
    cost cap or just let the task run longer."""
    step_limit = getattr(agent.config, "step_limit", 0)
    cost_limit = getattr(agent.config, "cost_limit", 0)
    step_hit = step_limit > 0 and agent.n_calls >= step_limit
    cost_hit = cost_limit > 0 and agent.cost >= cost_limit
    if step_hit and not cost_hit:
        return "iteration_limit_exceeded"
    if cost_hit and not step_hit:
        return "cost_limit_exceeded"
    return "budget_exceeded"


_ERROR_DETAIL_MAX_CHARS = 300


def _error_detail(exc: BaseException) -> str:
    return describe_llm_error(exc, _ERROR_DETAIL_MAX_CHARS)


def _is_transient_llm_error(exc: BaseException) -> bool:
    """A provider/network failure that outlived the retry layers (litellm's
    own plus mini-swe-agent's tenacity wrapper) but is worth trying again
    later: connection drops, SSL EOF, timeouts, 5xx, rate limits.

    The errors mini-swe-agent itself refuses to retry (auth, not-found,
    context window, unsupported params) are deliberately NOT transient —
    re-queuing them would just repeat the same failure."""
    import ssl

    import httpx
    import litellm
    from minisweagent.models.litellm_model import LitellmModel

    if isinstance(exc, tuple(LitellmModel.abort_exceptions)):
        return False
    # An exhausted spending limit arrives as a generic APIError, but no
    # retry before a human raises the limit (or it resets) can succeed.
    if budget_error(exc) is not None:
        return False
    ex = litellm.exceptions
    transient = (
        ex.APIError,
        ex.APIConnectionError,
        ex.InternalServerError,
        ex.ServiceUnavailableError,
        ex.BadGatewayError,
        ex.Timeout,
        ex.RateLimitError,
        httpx.TransportError,
        ssl.SSLError,
        ConnectionError,
        TimeoutError,
    )
    return isinstance(exc, transient)


def _fetch_comment_history(
    task: Task, settings: Settings, forge: ForgeClient
) -> list[Comment]:
    """The issue's recent comments, normalized, for the run context (R3.2).

    Best-effort (R3.6): any failure — bad credentials, the forge down —
    returns an empty list and the run still gets the triggering comment,
    which rode in on the webhook payload. A freshly ``opened`` issue has no
    thread, so that trigger is skipped entirely.
    """
    if task.trigger_kind is TriggerKind.ISSUE_OPENED:
        return []
    if settings.comments_context_max_comments <= 0:
        return []
    try:
        return forge.list_comments(
            task.issue_number, settings.comments_context_max_comments
        )
    except Exception:
        logger.warning(
            "comment_history_fetch_failed",
            task_id=task.id,
            issue_number=task.issue_number,
        )
        return []


# Outcomes where the agent did real, paid work that a resume can build on —
# the ones that get a WIP push and a block summary (R4.3, R5).
_BLOCK_ERRORS = {
    "task_timeout",
    "budget_exceeded",
    "iteration_limit_exceeded",
    "cost_limit_exceeded",
    "cost_unavailable",
}

# Failures that are not limit blocks but still leave a working tree worth
# keeping: the harness commits and pushes it to the work branch (same path as
# a block) before the workspace is deleted, so a new comment resumes instead
# of starting over. `llm_error` is a transient provider failure and is also
# re-queued automatically; the others wait for a human comment.
_SALVAGE_ERRORS = _BLOCK_ERRORS | {
    "llm_error", "agent_crashed", "format_error", "provider_budget_exceeded",
}


def _maybe_block_summary(
    agent, settings: Settings, workspace_path: str, error: str | None
) -> tuple[str | None, str, float]:
    """Summarize the interrupted attempt for the next run (R5). Best-effort:
    any failure leaves the block without a summary rather than crashing the
    task — the WIP on the branch is the real safety net. Returns
    ``(text_or_None, kind, extra_cost_usd)``."""
    if error not in _SALVAGE_ERRORS:
        return None, "", 0.0
    # No point summarizing (least of all paying for an LLM summary) when the
    # WIP that the summary accompanies is never going to be pushed.
    if not (settings.resume_enabled and settings.resume_push_wip_on_block):
        return None, "", 0.0
    try:
        from issue_agent.runner.summary import summarize_block

        text, kind, cost = summarize_block(
            # The model that just failed is the last thing to ask for a
            # handoff note: use the zero-cost mechanical one.
            policy=(
                "mechanical" if error in ("llm_error", "provider_budget_exceeded")
                else settings.resume_summary_on_block
            ),
            block_error=error,
            ws_path=Path(workspace_path),
            messages=list(getattr(agent, "messages", []) or []),
            model=agent.model,
            max_chars=settings.resume_summary_max_chars,
        )
        return (text or None), kind, cost
    except Exception:
        logger.warning("block_summary_failed", error=error)
        return None, "", 0.0


def _reap_agent_processes(agent, task: Task) -> None:
    """Kill whatever the agent left running once its run ends, on every exit
    path. Best-effort: containment must never turn a finished run into a crash."""
    try:
        killed = agent.env.reap()
    except Exception:
        logger.warning("agent_reap_failed", task_id=task.id, exc_info=True)
        return
    if killed:
        logger.warning("agent_processes_reaped_at_task_end", task_id=task.id, pids=killed)


class MiniSWERunner:
    def __init__(self, model_override=None) -> None:
        self._model = model_override

    def run(
        self,
        workspace_path: str,
        task: Task,
        settings: Settings,
        route: RepoRoute,
        resume_summary: str | None = None,
        on_progress: Callable[[int, float], None] | None = None,
        system_template: str | None = None,
        toolchain_env: dict[str, str] | None = None,
        branch_status: str | None = None,
        comment_history: list[Comment] | None = None,
        *,
        terms: ProviderTerms,
    ) -> AgentResult:
        from issue_agent.runner.agent_factory import build_agent
        from issue_agent.runner.litellm_cost import CostUnavailableError
        from issue_agent.runner.prompts import build_comments_block, build_user_message

        try:
            agent = build_agent(
                task, Path(workspace_path), settings, route,
                terms=terms, model=self._model, on_progress=on_progress,
                system_template=system_template,
                toolchain_env=toolchain_env,
            )
        except Exception as exc:
            logger.exception("agent_build_failed", task_id=task.id)
            return AgentResult(
                success=False, error="agent_crashed", error_detail=_error_detail(exc)
            )

        user_message = build_user_message(task, terms)

        # The discussion thread reaches the model as a Jinja *value* through
        # the static <comments> block (R3, D8) — never concatenated into
        # template text. The caller fetched it from the forge.
        agent.extra_template_vars["comments"] = build_comments_block(
            task, comment_history or [], settings
        )
        # The prior attempt's handoff note, same value-only path (R5.6). Set
        # only on a resume; agent_factory seeds it "" for the first attempt.
        if resume_summary:
            agent.extra_template_vars["resume_summary"] = resume_summary
        # What the harness's base merge did to a resumed branch, same
        # value-only path; seeded "" for a fresh branch.
        if branch_status:
            agent.extra_template_vars["branch_status"] = branch_status

        try:
            try:
                result = agent.run(task=user_message)
                exit_status = result.get("exit_status")
                submission = result.get("submission")
            finally:
                _reap_agent_processes(agent, task)
        except CostUnavailableError:
            # Strict cost tracking refused to proceed unmetered (R5.5.3) —
            # a terminal, alert-worthy stop, not a generic crash to retry.
            exit_status, submission = "cost_unavailable", None
        except ProviderBudgetExceededError as exc:
            # The provider's spending limit is exhausted: terminal and
            # alert-worthy, never re-queued — every retry is refused alike.
            logger.warning(
                "agent_run_provider_budget_exceeded",
                task_id=task.id,
                limit_source=exc.error.limit_source,
                resets_at=exc.error.resets_at,
                iterations=agent.n_calls,
            )
            error = "provider_budget_exceeded"
            summary, summary_kind, summary_cost = _maybe_block_summary(
                agent, settings, workspace_path, error
            )
            return AgentResult(
                success=False,
                error=error,
                error_detail=_error_detail(exc),
                provider_error=exc.error,
                cost_usd=agent.cost + summary_cost,
                iterations=agent.n_calls,
                block_summary=summary,
                block_summary_kind=summary_kind,
            )
        except Exception as exc:
            error = "llm_error" if _is_transient_llm_error(exc) else "agent_crashed"
            logger.warning(
                "agent_run_raised",
                task_id=task.id,
                error=error,
                exc_type=type(exc).__name__,
                iterations=agent.n_calls,
            )
            summary, summary_kind, summary_cost = _maybe_block_summary(
                agent, settings, workspace_path, error
            )
            return AgentResult(
                success=False,
                error=error,
                error_detail=_error_detail(exc),
                cost_usd=agent.cost + summary_cost,
                iterations=agent.n_calls,
                block_summary=summary,
                block_summary_kind=summary_kind,
            )

        if exit_status == "Submitted":
            return AgentResult(
                success=True,
                cost_usd=agent.cost,
                iterations=agent.n_calls,
                submission=submission,
            )

        if exit_status == "LimitsExceeded":
            error = _limits_exceeded_error(agent)
        else:
            error = _EXIT_STATUS_ERROR.get(exit_status, exit_status)
        summary, summary_kind, summary_cost = _maybe_block_summary(
            agent, settings, workspace_path, error
        )
        return AgentResult(
            success=False,
            error=error,
            cost_usd=agent.cost + summary_cost,
            iterations=agent.n_calls,
            submission=submission,
            block_summary=summary,
            block_summary_kind=summary_kind,
        )


def _resolve_agent_runner(settings: Settings) -> AgentRunner:
    return MiniSWERunner()


def _prepare_toolchain(
    workspace_path: Path,
    settings: Settings,
    route: RepoRoute,
    task_log,
) -> dict[str, str]:
    """Detect, install and cache the toolchain(s) needed by the workspace.

    Returns an env dict (usually an extended PATH) to inject into the agent's
    sandbox and into harness commands like the post-agent test runner.
    """
    if not settings.toolchains_enabled:
        return {}

    # Repo-level override: disable provisioning before paying for detection
    # (which can hit the network, e.g. Node.js release-index/checksum lookups).
    project_toolchain = route.project.toolchain.lower()
    if project_toolchain == "none":
        task_log.info("toolchain_disabled_by_project_config")
        return {}

    manager = ToolchainManager(
        settings.toolchains_tools_root,
        default_node_version=settings.toolchains_nodejs_default_version,
        nodejs_mirror=settings.toolchains_nodejs_mirror,
        install_timeout=settings.toolchains_install_timeout_seconds,
        lock_timeout=settings.toolchains_lock_timeout_seconds,
    )

    specs = manager.detect(workspace_path)

    if project_toolchain and project_toolchain != "auto":
        specs = [s for s in specs if s.name == project_toolchain]
        if not specs:
            task_log.warning(
                "toolchain_forced_but_not_detected",
                toolchain=project_toolchain,
                workspace=str(workspace_path),
            )

    if not specs:
        return {}

    try:
        toolchain_env = manager.ensure(specs)
    except Exception as exc:
        task_log.warning("toolchain_ensure_failed", error=str(exc))
        return {}

    if any(s.name == "nodejs" for s in specs):
        # Before the dependency install: `npm ci` is one of the heavy Node
        # processes on the small VM and runs under the same cap.
        toolchain_env = _cap_node_heap(toolchain_env, settings, task_log)

    install_project_dependencies(
        workspace_path,
        specs,
        toolchain_env,
        timeout_seconds=settings.toolchains_install_timeout_seconds,
    )

    return toolchain_env


def _cap_node_heap(
    toolchain_env: dict[str, str], settings: Settings, task_log
) -> dict[str, str]:
    """Put the capped ``node`` shim first on the toolchain PATH (see ``guards``).

    The cap is a share of this VM's memory (``resolve_node_heap_mb``). Best
    effort: a shim that cannot be written must never fail the task, it only
    means Node runs uncapped.
    """
    percent = settings.toolchains_nodejs_max_old_space_percent
    override_mb = settings.toolchains_nodejs_max_old_space_mb
    heap_mb = resolve_node_heap_mb(percent, override_mb)
    if heap_mb is None:
        task_log.info("node_heap_cap_disabled")
        return toolchain_env
    try:
        guarded = node_guard_env(toolchain_env, settings.toolchains_tools_root / "shims", heap_mb)
    except OSError as exc:
        task_log.warning("node_heap_cap_unavailable", error=str(exc))
        return toolchain_env
    task_log.info(
        "node_heap_cap_resolved",
        heap_mb=heap_mb,
        source=f"{override_mb} MB override" if override_mb > 0 else f"{percent}% of MemTotal",
    )
    return guarded


def _run_git(workspace_path: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=workspace_path,
        env={**os.environ, **env} if env else None,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {result.stderr.strip()}")
    return result.stdout.strip()


def _extract_issue_title(task: Task) -> str:
    trigger = task.trigger
    return (trigger.issue.title if trigger else "") or "(unknown)"


def _extract_author_login(task: Task) -> str:
    trigger = task.trigger
    return (trigger.actor.login if trigger else "") or "unknown"


def _parse_git_status(output: str) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    for line in output.split("\n"):
        line = line.strip()
        if not line:
            continue
        raw = line[2:].strip()
        if line[:2] == "??":
            files.append((raw, "added"))
        elif line[:2] == "R ":
            parts = raw.split(" -> ")
            files.append((parts[-1], "renamed"))
        elif line[:2] in ("M ", " M", "MM"):
            files.append((raw, "modified"))
        elif line[:2] in ("A ", " A"):
            files.append((raw, "added"))
        elif line[:2] in ("D ", " D"):
            files.append((raw, "deleted"))
        else:
            files.append((raw, line[:2]))
    return files


def _salvage_agent_commits(ws_path: Path, branch: str, start_sha: str, task_log) -> str:
    """Undo any commits the agent made on top of the branch's starting point,
    keeping their changes staged in the working tree.

    The agent is supposed to leave edits in the working tree for the harness
    to commit (see prompts/system.md). When it commits instead — usually
    because the target repo's CLAUDE.md/AGENTS.md tells it to — ``git status
    --porcelain`` comes back clean and the run looks like ``no_changes``.
    This detects that case and soft-resets the agent's commits so the normal
    ``git add -A`` / harness-commit / push flow owns the change set (and its
    cross-repo ``Closes`` reference). Returns the porcelain status after the
    reset (empty string when the agent genuinely produced nothing).

    ``start_sha`` is the branch tip the agent started from — NOT
    ``origin/<base>``. On a resumed run the work branch can be based on an
    older base tip; resetting to the current ``origin/<base>`` would leave the
    old tree under the new parent and silently revert everything the base
    gained in between (issue #86)."""
    try:
        ahead = _run_git(ws_path, "rev-list", "--count", f"{start_sha}..HEAD").strip()
    except Exception:
        return ""
    if ahead in ("", "0"):
        return ""

    task_log.warning("agent_self_committed", commits_ahead=ahead)
    # -B: point <branch> at wherever the agent's work ended up (it may have
    # switched branches) and check it out; then move the ref back to the
    # starting point with everything staged.
    _run_git(ws_path, "checkout", "-B", branch)
    _run_git(ws_path, "reset", "--soft", start_sha)
    return _run_git(ws_path, "status", "--porcelain")


def _head_sha(ws_path: Path) -> str:
    return _run_git(ws_path, "rev-parse", "HEAD")


class BranchIntegrityError(RuntimeError):
    """The branch tip would revert content that the base branch contains."""


def _assert_branch_integrity(ws_path: Path, base_branch: str, start_sha: str) -> None:
    """Refuse a tip whose tree reverts the base while its history says
    otherwise (parent = base tip, tree = an older snapshot).

    When ``origin/<base>`` is an ancestor of HEAD, every file that differs
    from the base tip must be one the branch legitimately changed: either it
    was already part of the PR when the run started (``base...start_sha``) or
    it changed during this run (``start_sha`` → HEAD). Anything else is
    content the base has and the branch silently dropped or rolled back."""
    base_ref = f"origin/{base_branch}"
    try:
        _run_git(ws_path, "merge-base", "--is-ancestor", base_ref, "HEAD")
    except RuntimeError:
        # Not based on the current base tip (e.g. the update merge conflicted):
        # a diff against the base would just show its newer commits.
        return

    def names(*args: str) -> set[str]:
        return set(_run_git(ws_path, "diff", "--name-only", *args).splitlines())

    expected = names(f"{base_ref}...{start_sha}") | names(start_sha, "HEAD")
    unexpected = sorted(names(base_ref, "HEAD") - expected)
    if unexpected:
        raise BranchIntegrityError(
            f"{len(unexpected)} file(s) differ from {base_ref} outside the branch's own "
            f"changes (first: {', '.join(unexpected[:5])})"
        )


def _commits_ahead_of_base(ws_path: Path, base_branch: str) -> int:
    """How many commits the current HEAD carries that ``origin/<base>`` does
    not. Zero when the count cannot be determined: the caller uses this to
    decide whether to push, and guessing "ahead" would push a branch we
    cannot reason about."""
    try:
        ahead = _run_git(
            ws_path, "rev-list", "--count", f"origin/{base_branch}..HEAD"
        ).strip()
    except Exception:
        return 0
    try:
        return int(ahead)
    except ValueError:
        return 0


def _changed_files_vs_base(ws_path: Path, base_branch: str) -> list[tuple[str, str]]:
    """The branch's own changes against ``origin/<base>``, in the same
    ``(path, kind)`` shape ``_parse_git_status`` produces.

    Three dots (merge-base diff) because that is the diff a reviewer sees on
    the PR, and because it stays correct when the base merge conflicted and
    was aborted — a two-dot diff would then also list the base's own newer
    commits as if the branch had reverted them."""
    try:
        output = _run_git(
            ws_path, "diff", "--name-status", f"origin/{base_branch}...HEAD"
        )
    except Exception:
        return []

    kinds = {"A": "added", "M": "modified", "D": "deleted"}
    files: list[tuple[str, str]] = []
    for line in output.split("\n"):
        parts = line.strip().split("\t")
        if len(parts) < 2 or not parts[0]:
            continue
        code = parts[0]
        if code.startswith("R"):
            files.append((parts[-1], "renamed"))
        else:
            files.append((parts[-1], kinds.get(code[0], code)))
    return files


@dataclass
class BaseUpdate:
    """What `_update_branch_with_base` did to a resumed work branch — drives
    the agent's ``<branch_status>`` block and whether the harness still has a
    merge to conclude after the agent's run.

    ``conflicted_files`` holds ``(path, kind)`` pairs, kind being a
    human-readable description such as ``"both modified"``."""

    status: str  # merged | conflicted | failed
    conflicted_files: list[tuple[str, str]] = field(default_factory=list)

    @property
    def conflicted(self) -> bool:
        return self.status == "conflicted"


# Which index stages an unmerged path has (1 = merge base, 2 = ours, 3 =
# theirs) says what kind of conflict it is.
_UNMERGED_KINDS: dict[frozenset[str], str] = {
    frozenset("123"): "both modified",
    frozenset("23"): "both added",
    frozenset("12"): "deleted by them",
    frozenset("13"): "deleted by us",
    frozenset("2"): "added by us",
    frozenset("3"): "added by them",
    frozenset("1"): "both deleted",
}

# Only the `<<<<<<<` / `>>>>>>>` fences: a bare `=======` line is also a
# Markdown setext heading underline, and a fence never survives on its own.
_CONFLICT_MARKER_RE = re.compile(r"^(?:<{7}|>{7})(?: |$)", re.MULTILINE)


def _unmerged_paths(ws_path: Path) -> list[tuple[str, str]]:
    """The paths a stopped merge left unmerged, with the kind of conflict."""
    stages: dict[str, set[str]] = {}
    for entry in _run_git(ws_path, "ls-files", "--unmerged", "-z").split("\0"):
        meta, sep, path = entry.partition("\t")
        if not sep:
            continue
        stages.setdefault(path, set()).add(meta.split()[-1])
    return [
        (path, _UNMERGED_KINDS.get(frozenset(found), "conflicted"))
        for path, found in sorted(stages.items())
    ]


def _update_branch_with_base(ws_path: Path, base_branch: str, task_log) -> BaseUpdate:
    """Merge the current ``origin/<base>`` into a resumed work branch.

    A merge (not a rebase/reset) keeps the target's tree intact and doesn't
    rewrite history. On a content conflict the merge is left *in progress* —
    markers in the working tree, ``MERGE_HEAD`` set — for the agent to resolve
    by editing files; `_finish_base_merge` records it afterwards. Hiding the
    conflict instead (aborting and handing the agent the stale branch) made
    the agent copy the base's commits in by hand, which never updates the
    branch and only adds conflicts. Any other failure
    aborts and leaves the branch as it was."""
    try:
        _run_git(ws_path, "merge", "--no-edit", f"origin/{base_branch}")
        task_log.info("branch_update_merged", base=base_branch)
        return BaseUpdate("merged")
    except Exception:
        pass
    try:
        conflicted = _unmerged_paths(ws_path)
    except Exception:
        conflicted = []
    if conflicted:
        task_log.warning(
            "branch_update_conflict", base=base_branch, files=[p for p, _ in conflicted]
        )
        return BaseUpdate("conflicted", conflicted)
    task_log.warning("branch_update_failed", base=base_branch)
    with contextlib.suppress(Exception):
        _run_git(ws_path, "merge", "--abort")
    return BaseUpdate("failed")


def _files_with_conflict_markers(ws_path: Path, paths: list[str]) -> list[str]:
    """The ``paths`` whose working-tree copy still carries a conflict fence.
    A path the agent deleted counts as resolved."""
    unresolved = []
    for path in paths:
        try:
            text = (ws_path / path).read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, IsADirectoryError):
            continue
        if _CONFLICT_MARKER_RE.search(text):
            unresolved.append(path)
    return unresolved


def _finish_base_merge(
    ws_path: Path,
    branch: str,
    base_branch: str,
    start_sha: str,
    update: BaseUpdate,
    task_log,
) -> list[str]:
    """Record the base merge the agent resolved as a two-parent merge commit.

    Returns the conflicted paths that still carry markers; nothing is
    committed then. Otherwise only the conflicted paths are taken from the
    working tree — every other path keeps the content git merged — so any
    further edit the agent made stays in the working tree for the normal
    harness commit on top. ``commit-tree`` with explicit parents keeps the
    merge a merge even when the agent committed (dropping ``MERGE_HEAD``):
    a single-parent commit carrying the base's content is exactly what leaves
    the PR conflicting."""
    _salvage_agent_commits(ws_path, branch, start_sha, task_log)
    paths = [path for path, _ in update.conflicted_files]
    unresolved = _files_with_conflict_markers(ws_path, paths)
    if unresolved:
        return unresolved

    _run_git(ws_path, "add", "-A", "--", *paths)
    tree = _run_git(ws_path, "write-tree")
    message = (
        f"Merge origin/{base_branch} into {branch}\n\n"
        "Conflicts resolved:\n" + "\n".join(f"- {path}" for path in paths)
    )
    merge_sha = _run_git(
        ws_path, "commit-tree", tree, "-p", start_sha, "-p", f"origin/{base_branch}",
        "-m", message,
    )
    _run_git(ws_path, "update-ref", f"refs/heads/{branch}", merge_sha)
    _run_git(ws_path, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    with contextlib.suppress(Exception):
        _run_git(ws_path, "merge", "--quit")
    task_log.info("base_merge_committed", base=base_branch, files=paths)
    return []


def _abandon_base_merge(ws_path: Path, start_sha: str) -> None:
    """Back out of a base merge that will not be recorded, so nothing of it
    is committed or pushed. ``reset --hard`` covers an agent that already
    dropped ``MERGE_HEAD``."""
    try:
        _run_git(ws_path, "merge", "--abort")
    except Exception:
        with contextlib.suppress(Exception):
            _run_git(ws_path, "reset", "--hard", start_sha)


TEST_COMMAND_SENTINEL_FILENAME = ".agent-test-command"
_TEST_OUTPUT_EXCERPT_CHARS = 4000


def _consume_test_command_sentinel(ws_path: Path) -> str | None:
    """Read the agent's ``.agent-test-command`` sentinel (if any) and delete it.

    This MUST run right after the agent's own run ends, and before
    ``git status``/``git add -A``/commit ever look at the workspace. The
    sentinel is written by the agent, inside the git repo root, as part of
    its own run — before executor.py's git bookkeeping starts. Left in
    place it would: (1) show up as an untracked file in
    ``git status --porcelain``, get classified "added" by
    ``_parse_git_status``, and get committed and pushed into the PR branch
    as a stray dotfile in every PR against a repo without an explicit
    ``test_command``; and (2) make an otherwise no-op run look "dirty"
    enough to slip past the ``no_changes`` abort. Reading its content here
    and deleting it immediately preserves the information (for
    ``_resolve_test_command``) while keeping it out of git entirely.
    """
    sentinel = ws_path / TEST_COMMAND_SENTINEL_FILENAME
    if not sentinel.is_file():
        return None

    try:
        content = sentinel.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        content = ""
    finally:
        sentinel.unlink(missing_ok=True)

    first_line = content.splitlines()[0].strip() if content else ""
    return first_line or None


CHECKS_SENTINEL_FILENAME = ".agent-checks"
_REPORTED_CHECK_STATUSES = ("passed", "failed", "not_run")
_MAX_REPORTED_CHECKS = 10
_MAX_CHECK_COMMAND_CHARS = 200
_MAX_CHECK_REASON_CHARS = 300
_CHECK_LINE_RE = re.compile(r"^\s*[-*]?\s*(?P<status>[a-z_ -]+?)\s*:\s*(?P<rest>.+)$", re.IGNORECASE)
# Only an em/en dash separates the reason: an ASCII ` -- ` is part of real
# commands (`npm test -- --forceExit`).
_CHECK_REASON_SEPARATOR_RE = re.compile(r"\s+[—–]\s+")


@dataclass(frozen=True)
class ReportedCheck:
    """One check the agent says it ran or attempted, read from
    ``.agent-checks``. This is the agent's own claim — the harness does not
    re-run it — so it is rendered apart from the harness-verified result."""

    status: str
    command: str
    reason: str = ""


def _clip(text: str, max_chars: int) -> str:
    return clip(text, max_chars)


def _parse_reported_checks(content: str) -> list[ReportedCheck]:
    """Parse ``<status>: <command>[ — <reason>]`` lines, dropping anything
    outside that contract. The file is model-authored and ends up in a public
    PR body, so the list and every field are capped, and backticks are
    stripped from the command so it cannot break out of its inline-code span."""
    checks: list[ReportedCheck] = []
    for line in content.splitlines():
        match = _CHECK_LINE_RE.match(line)
        if not match:
            continue
        status = match["status"].strip().lower().replace(" ", "_").replace("-", "_")
        if status not in _REPORTED_CHECK_STATUSES:
            continue
        command, *reason_part = _CHECK_REASON_SEPARATOR_RE.split(match["rest"], maxsplit=1)
        reason = reason_part[0] if reason_part else ""
        command = _clip(command.replace("`", ""), _MAX_CHECK_COMMAND_CHARS)
        if not command:
            continue
        checks.append(ReportedCheck(status, command, _clip(reason, _MAX_CHECK_REASON_CHARS)))
        if len(checks) == _MAX_REPORTED_CHECKS:
            break
    return checks


def _consume_checks_sentinel(ws_path: Path) -> list[ReportedCheck]:
    """Read the agent's ``.agent-checks`` sentinel (if any) and delete it.

    Same lifecycle as ``_consume_test_command_sentinel``, and for the same
    reasons: it must be gone before any git operation looks at the
    workspace. The content lists every check the agent ran or attempted —
    including the ones it could not finish — so the PR's ``How I tested``
    footer can report partial verification instead of claiming nothing ran
    (issue #102)."""
    sentinel = ws_path / CHECKS_SENTINEL_FILENAME
    if not sentinel.is_file():
        return []

    try:
        content = sentinel.read_text(encoding="utf-8", errors="replace")
    except OSError:
        content = ""
    finally:
        sentinel.unlink(missing_ok=True)

    return _parse_reported_checks(content)


def _resolve_test_command(project: ProjectProfile, sentinel_command: str | None) -> str | None:
    """Decide which command (if any) verifies the agent's changes.

    Priority order (A10 / R5.6.5): (1) ``project.test_command``, if
    explicitly configured on the resolved repo route — config always wins,
    even if the agent used something else; (2) ``sentinel_command``, the
    first line of the ``.agent-test-command`` sentinel file the agent may
    have written into the workspace root when the config was empty and it
    discovered/ran a command on its own (see ``prompts/system.md``), already
    extracted (and the file already deleted) by
    ``_consume_test_command_sentinel`` before this is called; (3) neither
    present — no command to run.
    """
    if project.test_command:
        return project.test_command

    return sentinel_command or None


def _run_test_command(
    ws_path: Path, command: str, timeout_seconds: int, toolchain_env: dict[str, str] | None = None
) -> tuple[bool, str]:
    """Run the resolved test command in the workspace and report pass/fail.

    This runs in the worker process, *after* the LLM agent's own run has
    ended — never as part of its bash loop — so ``testing_notes`` reflects
    an objective, code-driven result instead of the agent's self-report
    (R5.6.5). It reuses the same env allowlist (``_sandboxed_env``) and
    timeout/process-group-kill subprocess helper (``_run``) that the
    agent's own sandboxed bash commands use
    (``issue_agent.runner.environment``), rather than a fresh, unaudited
    ``subprocess.run`` call — the command may have been chosen by the LLM
    (via the sentinel file) and must not be handed the worker's secrets
    (GitHub App key, LLM API key, ...).

    ``toolchain_env`` extends the sandbox PATH so the harness can run
    project-specific test runners (e.g. ``npm test``) that were installed
    on demand into ``/data/tools``.
    """
    try:
        result = _run_sandboxed(
            command, str(ws_path), _sandboxed_env(toolchain_env or {}), timeout_seconds
        )
        output = result.stdout
        passed = result.returncode == 0
    except subprocess.TimeoutExpired as exc:
        raw = exc.output or ""
        output = f"{raw}\n[test command timed out after {timeout_seconds}s]"
        passed = False
    except Exception as exc:  # defensive: verification must never crash the task
        output = f"[error running test command: {exc}]"
        passed = False

    return passed, output[-_TEST_OUTPUT_EXCERPT_CHARS:]


_REPORTED_CHECK_LABELS = {"passed": "passed", "failed": "failed", "not_run": "not run"}


def _build_testing_notes(
    command: str | None,
    passed: bool | None,
    reported_checks: list[ReportedCheck] | tuple[ReportedCheck, ...] = (),
) -> str:
    """Build an honest ``## How I tested`` section — never a hardcoded success claim.

    Checkboxes are reserved for what the harness itself re-ran. The agent's
    own report (``.agent-checks``) follows in a separately labeled list of
    plain bullets, so a self-reported pass can never read as harness
    evidence, and partial verification no longer collapses into "testing was
    not performed" (issue #102)."""
    if command is None and not reported_checks:
        return (
            "- [ ] No test command was configured or discovered; "
            "testing was not performed."
        )
    if command is None:
        harness_line = "- [ ] The harness did not re-run any test command."
    elif passed:
        harness_line = f"- [x] `{command}` passed"
    else:
        harness_line = f"- [ ] `{command}` failed (see log)"
    if not reported_checks:
        return harness_line

    reported = "\n".join(
        f"- `{check.command}` — {_REPORTED_CHECK_LABELS[check.status]}"
        + (f": {check.reason}" if check.reason else "")
        for check in reported_checks
    )
    return f"{harness_line}\n\nReported by the agent (not re-run by the harness):\n{reported}"


_PR_DESCRIPTION_MAX_CHARS = 6000
_PR_DESCRIPTION_TRUNCATION_MARKER = "\n\n…(truncated)"


def _truncate_agent_text(text: str, max_chars: int = _PR_DESCRIPTION_MAX_CHARS) -> str:
    """Truncate agent-generated, otherwise-unbounded text to a safe length
    for a GitHub PR body or comment. Shared by `_resolve_pr_description` and
    `_resolve_agent_summary` (see their docstrings for why this matters)."""
    if len(text) > max_chars:
        cutoff = max_chars - len(_PR_DESCRIPTION_TRUNCATION_MARKER)
        return text[:cutoff].rstrip() + _PR_DESCRIPTION_TRUNCATION_MARKER
    return text


def _resolve_pr_description(submission: str | None, issue_title: str) -> str:
    """Pick the PR body's ``description``: the agent's own final report when
    it wrote one, else the issue title.

    ``prompts/system.md`` tells the agent its final submission "clearly
    summarizes what changed and why" because that text becomes the PR
    description — this is what actually makes that true, instead of the
    submission being silently discarded while ``description`` stays pinned
    to the issue title regardless of what the agent did.

    The submission is agent-generated and otherwise unbounded, while GitHub
    rejects PR bodies over 65,536 characters. Since the commit and push
    already happened by the time this feeds PR creation, an oversized body
    would fail PR creation *after* the branch is already pushed — leaving
    an orphan branch and a task marked recoverable-failure that just
    retries and wastes budget. Truncate well below that limit instead.
    """
    stripped = (submission or "").strip()
    return _truncate_agent_text(stripped or issue_title)


def _resolve_agent_summary(submission: str | None) -> str | None:
    """Pick a closing summary from the agent's own final report, for
    persisting on `issue_work.last_summary` (kind ``"submission"``) when a
    run closes with `no_changes` or `pr_opened` — so a later re-trigger on
    the same issue (a maintainer's follow-up comment) doesn't have to
    re-derive "what happened last time and why" from scratch. Unlike
    `_resolve_pr_description`, there is no issue-title fallback: an empty
    submission means no summary to persist, not a placeholder one."""
    stripped = (submission or "").strip()
    return _truncate_agent_text(stripped) if stripped else None


def _accumulated_note(work: IssueWork | None, result: AgentResult) -> str:
    """Extra line for the fix/abort comment showing the issue's total spend
    across every attempt, not just this one.

    ``result.cost_usd``/``iterations`` are per-attempt — a fresh
    ``MiniSWERunner`` (and its `agent.cost`) is built for every task/attempt,
    so on a resumed issue that number alone understates how much the issue
    has actually cost so far. Only shown once there has been a resume
    (``work.attempts > 0``) so a normal first-attempt success stays terse."""
    if work is None or work.attempts == 0:
        return ""
    total_cost = work.total_cost_usd + result.cost_usd
    total_iterations = work.total_iterations + result.iterations
    return (
        f"Total accumulated for this issue: US$ {total_cost:.2f} | "
        f"{total_iterations} iterations across {work.attempts + 1} attempts."
    )


def _build_fix_comment(
    task: Task,
    pr: PRRef,
    result: AgentResult,
    target_repo: str,
    agent_name: str,
    work: IssueWork | None = None,
    carried_over_commits: int = 0,
    branch: str | None = None,
    updated_existing: bool = False,
    still_conflicting: bool = False,
) -> str:
    body = f"Cost: US$ {result.cost_usd:.2f} | Iterations: {result.iterations}"
    note = _accumulated_note(work, result)
    if note:
        body += f"\n\n{note}"
    if still_conflicting:
        body += f"\n\n{_still_conflicting_note(task.base_branch)}"
    if updated_existing:
        # A follow-up pushed onto a PR that was already open: "opened a PR"
        # would misreport it.
        headline = (
            f"🤖 **{agent_name}** updated the PR in response to the latest "
            "comment. A summary of this update is on the PR."
        )
    elif carried_over_commits:
        # This run wrote nothing: the fix was already committed on the work
        # branch by an earlier run that never opened a PR (issue #89). Saying
        # "opened a PR with a proposed fix" would hide that, and the agent's
        # own report below reads like "nothing to do here".
        plural = "s" if carried_over_commits > 1 else ""
        headline = (
            f"🤖 **{agent_name}** opened a PR for work an earlier run had "
            "already committed.\n\n"
            f"This run produced no new edits; `{branch}` was "
            f"{carried_over_commits} commit{plural} ahead of `{task.base_branch}`."
        )
    else:
        headline = f"🤖 **{agent_name}** opened a PR with a proposed fix."
    return (
        f"{headline}\n\n"
        f"PR: [{target_repo}#{pr.number}]({pr.url})\n"
        f"{body}\n\n"
        f"---\nTask: `{task.id}` | _#{task.issue_number}_"
    )


def _still_conflicting_note(base_branch: str, subject: str = "this PR") -> str:
    return (
        f"⚠️ A check after this push still reports conflicts between {subject} "
        f"and `{base_branch}` — it needs a human to resolve them, or another "
        "comment to retry."
    )


def _base_update_lines(base_update: BaseUpdate | None, base: str) -> list[str]:
    """What the harness's merge of the base did, for a follow-up's report."""
    if base_update is None:
        return []
    if base_update.conflicted:
        resolved = ", ".join(f"`{path}`" for path, _ in base_update.conflicted_files)
        return [f"Merged `{base}` into the branch; conflicts resolved in {resolved}."]
    if base_update.status == "merged":
        return [f"Merged `{base}` into the branch."]
    if base_update.status == "failed":
        return [f"⚠️ Could not merge `{base}` into the branch."]
    return []


def _build_branch_published_comment(
    task: Task,
    branch: str,
    compare_url: str,
    result: AgentResult,
    description: str,
    changed_files: list[tuple[str, str]],
    testing_notes: str,
    agent_name: str,
    pr_term: str,
    work: IssueWork | None = None,
    carried_over_commits: int = 0,
    updated_existing: bool = False,
    base_update: BaseUpdate | None = None,
    still_conflicting: bool = False,
) -> str:
    """The issue comment for a run delivered on a forge with no PR API
    (PRD §3.7). The branch and its compare link stand in for the PR, so the
    summary a PR body would carry goes here too. It never promises the issue
    will close: nothing on the forge ties the branch to it."""
    base = task.base_branch
    if updated_existing:
        headline = (
            f"🤖 **{agent_name}** updated branch `{branch}` in response to the "
            "latest comment."
        )
    elif carried_over_commits:
        plural = "s" if carried_over_commits > 1 else ""
        headline = (
            f"🤖 **{agent_name}** published branch `{branch}` with work an "
            "earlier run had already committed.\n\n"
            f"This run produced no new edits; the branch was "
            f"{carried_over_commits} commit{plural} ahead of `{base}`."
        )
    else:
        headline = f"🤖 **{agent_name}** pushed a proposed fix to branch `{branch}`."
    lines = [headline]
    if updated_existing:
        lines.extend(_base_update_lines(base_update, base))
    if still_conflicting:
        lines.append(_still_conflicting_note(base, "the branch"))
    lines.append(
        f"No {pr_term} was opened: review the changes in "
        f"[`{base}...{branch}`]({compare_url}) and open one from the branch."
    )
    stats = f"Cost: US$ {result.cost_usd:.2f} | Iterations: {result.iterations}"
    note = _accumulated_note(work, result)
    if note:
        stats += f"\n\n{note}"
    files = "\n".join(f"- `{path}` — {kind}" for path, kind in changed_files) or "_None._"
    return (
        "\n\n".join(lines)
        + f"\n\n## Summary\n{description}"
        + f"\n\n## Files changed\n{files}"
        + f"\n\n## How I tested\n{testing_notes}"
        + f"\n\n{stats}\n\n"
        + f"---\nTask: `{task.id}` | _#{task.issue_number}_"
    )


def _build_pr_update_comment(
    task: Task,
    result: AgentResult,
    description: str,
    changed_files: list[tuple[str, str]],
    testing_notes: str,
    agent_name: str,
    base_update: BaseUpdate | None = None,
    still_conflicting: bool = False,
) -> str:
    """Posted on an already-open PR that a follow-up run pushed to. The PR
    body is left as the original run wrote it; this comment is the record of
    what the follow-up changed."""
    base = task.base_branch
    lines = [
        f"🤖 **{agent_name}** updated this PR in response to a comment on "
        f"{task.repo}#{task.issue_number}."
    ]
    lines.extend(_base_update_lines(base_update, base))
    if still_conflicting:
        lines.append(_still_conflicting_note(base))
    files = "\n".join(f"- `{path}` — {kind}" for path, kind in changed_files) or "_None._"
    return (
        "\n\n".join(lines)
        + f"\n\n## Summary\n{description}"
        + f"\n\n## Files changed\n{files}"
        + f"\n\n## How I tested\n{testing_notes}"
        + f"\n\n---\nTask: `{task.id}`. Cost: US$ {result.cost_usd:.2f}. "
        f"Iterations: {result.iterations}."
    )


# Errors whose `error_detail` is rendered under the reason, and its label.
_DETAIL_LABELS = {
    "llm_error": "Provider error",
    "provider_budget_exceeded": "Provider message",
    "agent_crashed": "Error",
}


def _build_abort_comment(
    task: Task, result: AgentResult, agent_name: str, work: IssueWork | None = None
) -> str:
    reasons = {
        "budget_exceeded": "The maximum budget per task was exceeded.",
        "iteration_limit_exceeded": "The maximum number of iterations per task was exceeded.",
        "cost_limit_exceeded": "The maximum cost (budget) per task was exceeded.",
        "task_timeout": "The maximum task execution time was exceeded.",
        # Not a failure: the agent ran to completion and concluded nothing
        # needed changing. Its own report — which prompts/system.md requires
        # to cite the commit that already landed the fix — is appended
        # right below, so this line frames the verdict instead of reading
        # like an error.
        "no_changes": "The agent concluded that no code change was needed.",
        "format_error": (
            "The model replied outside the expected format several times "
            "in a row and execution was stopped."
        ),
        "cost_unavailable": (
            "The cost of the model calls could not be determined "
            "(the provider did not report cost and the model is not in the "
            "price map). The task was aborted to avoid running without "
            "budget control."
        ),
    }
    resets_at = result.provider_error.resets_at if result.provider_error else ""
    reset_note = f" on {resets_at[:10]}" if resets_at else ""
    reasons["provider_budget_exceeded"] = (
        "The LLM provider's spending limit was reached, so the run was "
        "stopped without retrying. Raise the limit (or wait for it to "
        f"reset{reset_note}) and comment on this issue to resume."
    )
    reasons["llm_error"] = "The LLM provider call kept failing after several retries."
    reasons["agent_crashed"] = "The agent crashed unexpectedly."
    reason = reasons.get(result.error or "", f"Error: {result.error}")
    # The detail goes on its own quoted line, not in parentheses inside the
    # sentence: it is free text from the provider (it may itself hold
    # brackets, or end in a cut), and a newline in it would end the quote.
    if result.error_detail and result.error in _DETAIL_LABELS:
        detail = " ".join(result.error_detail.split())
        reason += f"\n\n> {_DETAIL_LABELS[result.error]}: {detail}"
    body = (
        f"{reason}\n\n"
        f"Accumulated cost: US$ {result.cost_usd:.2f} | Iterations: {result.iterations}"
    )
    if result.error == "no_changes":
        summary = _resolve_agent_summary(result.submission)
        if summary:
            body += f"\n\n{summary}"
    note = _accumulated_note(work, result)
    if note:
        body += f"\n\n{note}"
    headline = (
        "run finished with no changes"
        if result.error == "no_changes"
        else "execution aborted"
    )
    return (
        f"🤖 **{agent_name}** — {headline}.\n\n"
        f"{body}\n\n"
        f"---\nTask: `{task.id}` | _#{task.issue_number}_"
    )


@dataclass
class WipOutcome:
    """What `_preserve_wip_on_block` did with the partial work — drives the
    block comment (R4.6, R4.7)."""

    reason: str = "disabled"  # disabled | no_changes | pushed | push_failed | max_attempts
    branch: str = ""

    @property
    def pushed(self) -> bool:
        return self.reason in ("pushed", "max_attempts")


def _build_block_comment(
    task: Task,
    result: AgentResult,
    agent_name: str,
    wip: WipOutcome,
    max_attempts: int,
    work: IssueWork | None = None,
    will_retry: bool = False,
) -> str:
    """The abort comment for a block, enriched with what happened to the WIP.

    Reuses `_build_abort_comment` for the reason/cost lines and splices a
    line about the preserved (or lost) partial work in before the footer."""
    base = _build_abort_comment(task, result, agent_name, work)
    if wip.reason == "max_attempts":
        extra = (
            f"\n\n**Limit of {max_attempts} attempts reached — giving up.** "
            f"The partial work is on branch `{wip.branch}` for human inspection."
        )
    elif wip.reason == "pushed" and will_retry:
        extra = (
            f"\n\nThe partial work was preserved on branch `{wip.branch}`. "
            f"The task will be retried automatically shortly and continue "
            f"from where it left off; commenting on the issue resumes it "
            f"right away."
        )
    elif wip.reason == "pushed":
        extra = (
            f"\n\nThe partial work was preserved on branch `{wip.branch}`. "
            f"The next trigger continues from where it left off."
        )
    elif wip.reason == "push_failed":
        retry_note = (
            "The task will be retried automatically, but it starts over "
            "from scratch" if will_retry
            else "The next trigger starts over from scratch"
        )
        extra = (
            "\n\n⚠️ **Could not preserve the partial work** (the push "
            f"failed). {retry_note} — the compute and tokens from this "
            "attempt were lost."
        )
    else:
        extra = ""
    return base.replace("\n\n---\nTask:", f"{extra}\n\n---\nTask:", 1)


@dataclass(frozen=True)
class _Outbound:
    """The task's provider, bound to this task: comments go to the issue, PRs
    and git to the target repo. Every body passes through here, cut to the
    forge's limit (``capabilities.max_body_length``)."""

    task: Task
    forge: ForgeClient
    pulls: PullRequestClient | None
    transport: GitTransport
    capabilities: ProviderCapabilities

    @classmethod
    def for_task(cls, plugin: ProviderPlugin, task: Task) -> _Outbound:
        return cls(
            task=task,
            forge=plugin.client(task.repo),
            pulls=plugin.pulls(task.target_repo),
            transport=plugin.transport(),
            capabilities=plugin.capabilities,
        )

    @property
    def delivery_reason(self) -> str:
        """How a delivered run closes its work item: ``pr_opened``, or
        ``branch_published`` on a forge with no PR API (PRD §3.7). Chosen
        once per task, by whether the plugin has a ``PullRequestClient``."""
        return "pr_opened" if self.pulls is not None else "branch_published"

    def _fit(self, body: str) -> str:
        return pr_text.fit_body(body, self.capabilities.max_body_length)

    def comment(self, body: str) -> str:
        return self.forge.post_comment(self.task.issue_number, self._fit(body))

    def compare_url(self, branch: str) -> str:
        """The forge's diff of ``branch`` against the task's base."""
        return self.transport.compare_url(self.task.target_repo, self.task.base_branch, branch)

    def label_issue(self, labels: list[str]) -> None:
        """Labels a PR would carry, put on the issue instead — only where the
        forge has labels at all."""
        if self.capabilities.supports_labels and labels:
            self.forge.add_labels(self.task.issue_number, labels)

    def git_env(self) -> dict[str, str]:
        """A fresh credential for the target repo, scoped to git's env."""
        return self.transport.git_auth_env(self.task.target_repo)

    def issue_ref(self) -> str:
        """The issue as text in the target repo (commit, PR title)."""
        return self.transport.format_ref(
            "issue", self.task.issue_number,
            repo=self.task.repo, from_repo=self.task.target_repo,
        )

    def closing_reference(self) -> str | None:
        return self.transport.closing_reference(
            self.task.repo, self.task.issue_number, self.task.target_repo
        )

    def open_pr(
        self, *, head: str, base: str, title: str, body: str, draft: bool, labels: list[str],
    ) -> PRRef:
        """The open PR for ``head``, or a new one. An existing PR is returned
        untouched — a follow-up reports its update in a PR comment."""
        assert self.pulls is not None  # run_task refuses a forge without a PR API
        existing = self.pulls.find_open_pr(head)
        if existing is not None:
            return existing
        caps = self.capabilities
        if draft and caps.draft_mode == "title_prefix":
            title = f"{caps.draft_prefix or ''}{title}"
        return self.pulls.create_pr(
            head=head, base=base, title=title, body=self._fit(body),
            draft=draft and caps.draft_mode == "native", labels=labels,
        )

    def comment_on_pr(self, pr: PRRef, body: str) -> str:
        assert self.pulls is not None
        return self.pulls.comment_on_pr(pr, self._fit(body))


def _finish_salvaged_failure(
    store: SQLiteStore,
    task: Task,
    settings: Settings,
    out: _Outbound,
    work: IssueWork,
    wip: WipOutcome,
    result: AgentResult,
    total_cost: float,
    total_iterations: int,
    duration: float,
    task_log,
) -> None:
    """Close out a non-limit failure after `_preserve_wip_on_block` ran.

    Only a transient ``llm_error`` whose work stays open is re-queued by the
    worker (``FAILED_RECOVERABLE`` → ``run_retry_recovery``); everything else
    is terminal for this task, but the branch and the open ``issue_work`` row
    still let a new comment resume the work."""
    work_stays_open = wip.reason in ("pushed", "push_failed")
    will_retry = result.error == "llm_error" and work_stays_open
    status = TaskStatus.FAILED_RECOVERABLE if will_retry else TaskStatus.FAILED_TERMINAL
    _fail(
        store, task, settings, status,
        {"from": "running", "to": status.value, "error": result.error},
        error=result.error, cost_usd=total_cost, iterations=total_iterations,
        duration=duration, work=work,
    )
    if result.error == "provider_budget_exceeded":
        provider = result.provider_error
        try:
            send_budget_alert(
                settings.alert_webhook_url,
                "provider_budget_exceeded",
                spent_usd=(provider.spent_usd if provider else None) or 0.0,
                limit_usd=(provider.limit_usd if provider else None) or 0.0,
                task_id=task.id,
                issue_number=task.issue_number,
                repo=task.repo,
            )
        except Exception:
            task_log.exception("provider_budget_alert_failed")
    if wip.reason == "disabled":
        # resume is off: nothing was preserved, so there is nothing to resume.
        _close_issue_work(
            store, task, "terminal_failure",
            total_cost_usd=work.total_cost_usd + total_cost,
            total_iterations=work.total_iterations + total_iterations,
        )
    try:
        comment = _build_block_comment(
            task, result, settings.agent_name, wip,
            settings.resume_max_attempts, work, will_retry=will_retry,
        )
        out.comment(comment)
    except Exception:
        task_log.exception("abort_comment_failed")


def _build_merge_unresolved_comment(
    task: Task, unresolved: list[str], cost_usd: float, iterations: int, agent_name: str,
) -> str:
    files = "\n".join(f"- `{path}`" for path in unresolved)
    return (
        f"🤖 **{agent_name}** — could not bring the branch up to date with "
        f"`{task.base_branch}`.\n\n"
        f"Merging `{task.base_branch}` conflicted and these files still had "
        f"conflict markers when the agent finished:\n{files}\n\n"
        "Nothing was pushed: the branch and its PR are as they were. Comment "
        "again to retry, or resolve the conflicts by hand.\n"
        f"Cost: US$ {cost_usd:.2f} | Iterations: {iterations}\n\n"
        f"---\nTask: `{task.id}` | _#{task.issue_number}_"
    )


def _fail_unresolved_base_merge(
    store: SQLiteStore,
    task: Task,
    settings: Settings,
    out: _Outbound,
    work: IssueWork,
    force_continue: bool,
    unresolved: list[str],
    cost_usd: float,
    iterations: int,
    duration: float,
    task_log,
) -> None:
    """Stop a run whose base merge the agent left unresolved, keeping the
    work item resumable: a follow-up goes back to `pr_opened` (or
    `branch_published`) so the next comment reopens the same branch (any
    other closed reason would restart from the base and force-push over the
    delivered work), a block-retry stays open."""
    task_log.warning("base_merge_unresolved", files=unresolved)
    _fail(
        store, task, settings, TaskStatus.ABORTED_POLICY,
        {"from": "running", "to": "aborted_policy", "reason": "merge_conflict_unresolved"},
        error="merge_conflict_unresolved", cost_usd=cost_usd, iterations=iterations,
        duration=duration, work=work,
    )
    totals = {
        "total_cost_usd": work.total_cost_usd + cost_usd,
        "total_iterations": work.total_iterations + iterations,
    }
    if force_continue:
        _close_issue_work(
            store, task, out.delivery_reason, pr_number=work.pr_number, **totals
        )
    else:
        store.update_issue_work(
            task.provider_instance, task.repo, task.issue_number,
            last_task_id=task.id, last_error="merge_conflict_unresolved", **totals,
        )
    try:
        out.comment(
            _build_merge_unresolved_comment(
                task, unresolved, cost_usd, iterations, settings.agent_name,
            ),
        )
    except Exception:
        task_log.exception("abort_comment_failed")


def _build_test_retry_comment(
    task: Task,
    work: IssueWork,
    test_command: str,
    test_output_excerpt: str,
    agent_name: str,
    max_attempts: int,
) -> str:
    """Posted each time the harness's own post-push test verification fails
    and an automatic retry fires (`_decide_test_retry`) — transparency for a
    human watching the issue, matching the project's comment culture for
    every other state change. `work` is the row already refreshed by
    `_record_test_retry_attempt`, so `work.attempts` is this retry's own
    number."""
    return (
        f"🤖 **{agent_name}** pushed a fix, but the verification command "
        f"failed — retrying automatically (attempt {work.attempts}/{max_attempts}).\n\n"
        f"Command: `{test_command}`\n\n"
        f"<details><summary>Output (truncated)</summary>\n\n```\n{test_output_excerpt}\n```\n</details>\n\n"
        f"---\nTask: `{task.id}` | _#{task.issue_number}_"
    )


# One-line description of why the previous attempt stopped, for the
# continuation comment (R1.4). Keyed by the `last_error` persisted on
# issue_work when a run blocked.
_PRIOR_STOP_REASONS: dict[str, str] = {
    "budget_exceeded": "the per-task budget was exceeded",
    "iteration_limit_exceeded": "the per-task iteration limit was exceeded",
    "cost_limit_exceeded": "the per-task cost limit was exceeded",
    "task_timeout": "the maximum execution time was exceeded",
    "cost_unavailable": "the cost of the model calls could not be determined",
    "llm_error": "the LLM provider kept failing",
    "provider_budget_exceeded": "the LLM provider's spending limit was reached",
    "agent_crashed": "the agent crashed unexpectedly",
    "format_error": "the model kept replying outside the expected format",
    "test_failed": "the post-push test verification failed",
    "merge_conflict_unresolved": "conflicts merging the base branch were left unresolved",
}


def _build_initial_comment(
    task: Task,
    work: IssueWork,
    agent_name: str,
    pr_number: int | None = None,
    published_branch: bool = False,
) -> str:
    """Announce the start (or resumption) of work on an issue (R1.1, R1.4).

    Carries the attempt number, the frozen work branch and the task id.
    `work.attempts` is 0 on the first run and incremented on each resume;
    the human-facing attempt number is `attempts + 1`. ``pr_number`` is set
    on a follow-up to an already-open PR, ``published_branch`` on one to a
    branch published without a PR (PRD §3.7); neither is a fresh start.
    """
    attempt = work.attempts + 1
    if pr_number:
        lead = f"🤖 **{agent_name}** resumed working on this issue to update PR #{pr_number}."
    elif published_branch:
        lead = (
            f"🤖 **{agent_name}** resumed working on this issue to update its "
            "published branch."
        )
    elif work.attempts == 0:
        lead = f"🤖 **{agent_name}** started working on this issue."
    else:
        reason = _PRIOR_STOP_REASONS.get(work.last_error or "")
        tail = f" — previous stop: {reason}" if reason else ""
        lead = (
            f"🤖 **{agent_name}** resumed working on this issue "
            f"(attempt {attempt}){tail}."
        )
    return (
        f"{lead}\n\n"
        f"Work branch: `{work.work_branch}`\n"
        f"Attempt: {attempt}\n\n"
        f"---\nTask: `{task.id}` | _#{task.issue_number}_"
    )


def _build_progress_comment(
    task: Task, iteration: int, cost_usd: float, agent_name: str
) -> str:
    """The "still working" comment posted mid-run every N iterations (R6.1)."""
    return (
        f"🤖 **{agent_name}** is still working on this issue.\n\n"
        f"Iteration: {iteration} | Accumulated cost: US$ {cost_usd:.2f}\n\n"
        f"---\nTask: `{task.id}` | _#{task.issue_number}_"
    )


def _make_progress_reporter(
    out: _Outbound, task: Task, agent_name: str, task_log
) -> Callable[[int, float], None]:
    """A callback the agent invokes mid-run to post a progress comment. The
    subclass already caps frequency and count and swallows errors (R6.3,
    R6.4); this only builds the text and posts it."""

    def _report(iteration: int, cost_usd: float) -> None:
        out.comment(_build_progress_comment(task, iteration, cost_usd, agent_name))
        task_log.info("progress_comment_posted", iteration=iteration)

    return _report


def _open_or_resume_issue_work(
    store: SQLiteStore,
    task: Task,
    settings: Settings,
    route: RepoRoute,
    issue_title: str,
) -> tuple[IssueWork, bool, bool]:
    """Get the `issue_work` row for this issue, opening it on the first FIX
    run and resuming it on later ones (R4.1). Returns
    ``(work, resumed, force_continue)``.

    The work branch is computed once here — from the issue title in effect
    at open time — and frozen on the row; every subsequent run reads it back
    instead of recomputing from a payload that may have changed (C4). A row
    left `closed` is normally treated as absent (a new work item starts
    fresh) — with one exception: a row closed as `pr_opened` (or, on a forge
    with no PR API, `branch_published` — PRD §3.7) is reopened *in place*
    instead, so a maintainer's follow-up comment continues from the
    delivered branch rather than re-cloning and re-doing the work from
    scratch. `force_continue` is True only for that reopen — it tells
    the caller to check out the existing remote branch and inject the prior
    `last_summary` regardless of `resume.enabled`, since this is a distinct
    feature from the block-retry resume machinery that flag otherwise
    gates. `attempts` is deliberately *not* bumped on this path — that
    counter is a block-retry budget (`resume.max_attempts`), and a
    human-driven PR follow-up isn't a block retry.
    """
    def _branch() -> str:
        return pr_text.branch_name(
            task.issue_number,
            issue_title,
            task.workflow,
            route.branch_prefix,
            task.base_branch,
            slug_max_length=settings.pr_slug_max_length,
        )

    work = store.get_issue_work(task.provider_instance, task.repo, task.issue_number)

    if (
        work is not None and work.state == "closed"
        and work.closed_reason in _DELIVERED_REASONS
    ):
        delivered_as = work.closed_reason
        store.update_issue_work(
            task.provider_instance, task.repo, task.issue_number,
            state="open", closed_reason=None, last_task_id=task.id,
        )
        work = store.get_issue_work(task.provider_instance, task.repo, task.issue_number)
        assert work is not None  # just updated
        store.append_task_event(
            task.id,
            "issue_work_reopened_from_pr"
            if delivered_as == "pr_opened"
            else "issue_work_reopened_from_branch",
            {"work_branch": work.work_branch, "pr_number": work.pr_number},
        )
        return work, True, True

    if work is None or work.state == "closed":
        branch = _branch()
        if work is not None:
            # A closed row (no_changes/max_attempts/terminal_failure — the
            # delivered cases are handled above, and R7.6 already intercepted
            # the one closed_reason, `issue_closed`, that must NOT restart)
            # is a spent work item — the PK forbids a second `open`, so drop
            # it and start fresh.
            store.delete_issue_work(task.provider_instance, task.repo, task.issue_number)
        work = store.open_issue_work(
            task.provider_instance, task.repo, task.issue_number, task.target_repo, task.base_branch, branch
        )
        store.append_task_event(
            task.id,
            "issue_work_opened",
            {"work_branch": branch, "attempts": work.attempts},
        )
        return work, False, False

    updates: dict = {"attempts": work.attempts + 1, "last_task_id": task.id}
    # A blank work_branch means a prior WIP push failed and the row was reset
    # for a clean restart (R4.4): recompute the branch from the current title
    # now, keeping the attempts counter (and the max_attempts cap) intact.
    if not work.work_branch:
        updates["work_branch"] = _branch()
    store.update_issue_work(task.provider_instance, task.repo, task.issue_number, **updates)
    work = store.get_issue_work(task.provider_instance, task.repo, task.issue_number)
    assert work is not None  # just updated
    store.append_task_event(
        task.id,
        "issue_work_resumed",
        {"work_branch": work.work_branch, "attempts": work.attempts},
    )
    observe_resume(task.target_repo, provider_instance=task.provider_instance)
    return work, True, False


# The closed reasons of a delivered run, which a follow-up comment reopens.
_DELIVERED_REASONS = ("pr_opened", "branch_published")


def _local_conflicts(
    ws_path: Path, base_branch: str, git_env: dict[str, str] | None = None
) -> bool | None:
    """Whether ``HEAD`` would conflict merging into ``origin/<base>``, worked
    out in the workspace with ``git merge-tree`` and no forge involved.

    The source of truth on a forge with no PR API, and the fallback when the
    forge can't say (PRD §3.7). With ``git_env`` the base is fetched first,
    best-effort, so a base that moved during the run is what gets compared.
    ``None`` when git can't tell — an unrelated history, a missing ref."""
    if git_env is not None:
        with contextlib.suppress(Exception):
            _run_git(ws_path, "fetch", "origin", base_branch, env=git_env)
    try:
        result = subprocess.run(
            ["git", "merge-tree", "--write-tree", f"origin/{base_branch}", "HEAD"],
            capture_output=True,
            text=True,
            cwd=ws_path,
        )
    except Exception:
        return None
    if result.returncode == 0:
        return False
    # Exit 1 is both "conflicts" and some errors (a ref git can't resolve);
    # only a conflict prints the merged tree's id first.
    first_line = result.stdout.split("\n", 1)[0].strip()
    if result.returncode == 1 and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", first_line):
        return True
    return None


def _still_conflicting(
    out: _Outbound, pr: PRRef | None, pushed_sha: str, ws_path: Path, base_branch: str,
    task_log,
) -> bool:
    """Whether the delivered branch still conflicts with its base after the
    push: the forge's answer for the PR when it has one, the workspace's
    otherwise."""
    if pr is not None and out.pulls is not None:
        try:
            answer = out.pulls.pr_has_conflicts(pr, pushed_sha)
        except Exception:
            task_log.warning("pr_mergeable_check_failed")
            answer = None
        if answer is not None:
            return answer is True
    try:
        git_env = out.git_env()
    except Exception:
        git_env = None
    return _local_conflicts(ws_path, base_branch, git_env) is True


def _close_issue_work(
    store: SQLiteStore,
    task: Task,
    closed_reason: str,
    pr_number: int | None = None,
    total_cost_usd: float | None = None,
    total_iterations: int | None = None,
) -> None:
    store.close_issue_work(
        task.provider_instance, task.repo,
        task.issue_number,
        closed_reason,
        pr_number=pr_number,
        total_cost_usd=total_cost_usd,
        total_iterations=total_iterations,
    )
    closed = store.get_issue_work(task.provider_instance, task.repo, task.issue_number)
    attempts = closed.attempts if closed else 0
    store.append_task_event(
        task.id,
        "issue_work_closed",
        {"closed_reason": closed_reason, "attempts": attempts},
    )
    observe_issue_work_closed(
        task.target_repo, attempts, provider_instance=task.provider_instance
    )


def _remote_branch_exists(ws_path: Path, branch: str, git_env: dict[str, str]) -> bool:
    """Whether ``branch`` is on the target remote — decides resume-from-branch
    vs. start-from-base (R4.2). Any failure is treated as "not there", so a
    flaky ``ls-remote`` degrades to a clean restart rather than a crash."""
    try:
        out = _run_git(
            ws_path,
            "ls-remote",
            "--heads",
            "origin",
            f"refs/heads/{branch}",
            env=git_env,
        )
        return bool(out.strip())
    except Exception:
        return False


def _preserve_wip_on_block(
    store: SQLiteStore,
    task: Task,
    settings: Settings,
    work: IssueWork,
    ws_path: Path,
    branch: str,
    result: AgentResult,
    out: _Outbound,
    task_log,
    start_sha: str,
) -> WipOutcome:
    """Commit and push the partial work to the frozen branch before cleanup,
    persist the block summary, and decide whether the work stays open, closes
    with ``no_changes`` (nothing to resume, R4.5), or closes with
    ``max_attempts`` (retry ceiling, R4.7).

    The block is always recorded on the row (``last_error`` / ``last_task_id``)
    so ``resume.enabled: false`` keeps the behavior from before resume existed."""
    attempt = work.attempts + 1
    base_updates: dict = {
        "last_task_id": task.id,
        "last_error": result.error,
        "total_cost_usd": work.total_cost_usd + result.cost_usd,
        "total_iterations": work.total_iterations + result.iterations,
    }

    if not (settings.resume_enabled and settings.resume_push_wip_on_block):
        store.update_issue_work(task.provider_instance, task.repo, task.issue_number, **base_updates)
        return WipOutcome(reason="disabled")

    try:
        _run_git(ws_path, "add", "-A")
        staged = _run_git(ws_path, "status", "--porcelain")
    except Exception:
        staged = ""

    has_wip = bool(staged.strip())
    if not has_wip:
        try:
            ahead = _run_git(
                ws_path, "rev-list", "--count", f"origin/{task.base_branch}..HEAD"
            ).strip()
        except Exception:
            ahead = ""
        if ahead in ("", "0"):
            store.update_issue_work(
                task.provider_instance, task.repo,
                task.issue_number,
                last_summary=result.block_summary or "",
                last_summary_kind=result.block_summary_kind,
                **base_updates,
            )
            _close_issue_work(store, task, "no_changes")
            return WipOutcome(reason="no_changes")

    try:
        if has_wip:
            _run_git(
                ws_path,
                "commit",
                "-m",
                f"WIP: issue #{task.issue_number} attempt {attempt} "
                f"({result.error}) [skip ci]",
            )
        _assert_branch_integrity(ws_path, task.base_branch, start_sha)
        _run_git(
            ws_path, "push", "-f", "origin", branch, env=out.git_env(),
        )
    except Exception:
        task_log.exception("wip_push_failed")
        store.update_issue_work(
            task.provider_instance, task.repo, task.issue_number, work_branch="", **base_updates
        )
        return WipOutcome(reason="push_failed")

    store.update_issue_work(
        task.provider_instance, task.repo,
        task.issue_number,
        last_summary=result.block_summary or "",
        last_summary_kind=result.block_summary_kind,
        **base_updates,
    )

    if attempt >= settings.resume_max_attempts:
        _close_issue_work(store, task, "max_attempts")
        return WipOutcome(reason="max_attempts", branch=branch)

    return WipOutcome(reason="pushed", branch=branch)


def _decide_test_retry(
    store: SQLiteStore, task: Task, settings: Settings, work: IssueWork,
) -> tuple[bool, str]:
    """Whether an automatic in-loop retry may fire after the harness's own
    post-push test verification failed. Reuses the SAME attempts counter and
    cap the block-retry machinery uses (``work.attempts`` / `resume_max_attempts`,
    see `_preserve_wip_on_block` above) rather than a parallel counter, plus a
    fresh daily-budget check — the one guard normally checked only once,
    before a task is even dequeued (`policy.guards.check_daily_budget`,
    called from `worker.run_task`) — since a loop inside a single task could
    otherwise spend past it unnoticed until the next task is picked up.
    Returns ``(may_retry, stop_reason)`` — ``stop_reason`` is only meaningful
    when ``may_retry`` is ``False``."""
    if work.attempts + 1 >= settings.resume_max_attempts:
        return False, "max_attempts"
    ok, _ = check_daily_budget(store, settings, task.target_repo)
    if not ok:
        return False, "daily_budget_exceeded"
    return True, ""


def _record_test_retry_attempt(
    store: SQLiteStore,
    task: Task,
    work: IssueWork,
    test_command: str,
    test_output_excerpt: str,
) -> IssueWork:
    """Persist the retry as an attempt against the same `resume_max_attempts`
    budget the block-retry machinery uses — no parallel counter. Deliberately
    does not touch `total_cost_usd`/`total_iterations`: those stay "everything
    before this task's own loop" and are folded in once, at the very end, by
    `run_task`'s `accumulated_cost`/`accumulated_iterations` (avoids double-
    counting a retried iteration's spend)."""
    store.update_issue_work(
        task.provider_instance, task.repo, task.issue_number,
        attempts=work.attempts + 1,
        last_task_id=task.id,
        last_error="test_failed",
    )
    store.append_task_event(
        task.id, "issue_work_test_retry",
        {
            "attempt": work.attempts + 1,
            "test_command": test_command,
            "output_excerpt": test_output_excerpt[:2000],
        },
    )
    refreshed = store.get_issue_work(task.provider_instance, task.repo, task.issue_number)
    assert refreshed is not None  # just updated
    return refreshed


def _build_test_failure_resume_summary(test_command: str, test_output_excerpt: str) -> str:
    """Fed as `resume_summary` into the next loop iteration's `runner.run()`
    call — the same extension point used for block-then-human-resume, reused
    here to tell the agent its own test command failed and why."""
    return (
        "Your previous attempt was committed and pushed to this branch, but "
        "the harness's post-push verification command failed:\n\n"
        f"$ {test_command}\n"
        f"{test_output_excerpt}\n\n"
        "Fix the failure above. Build on the work already committed — do not "
        "revert or discard it."
    )


_PROMETHEUS_LABEL: dict[TaskStatus, str] = {
    TaskStatus.FAILED_RECOVERABLE: "failed",
    TaskStatus.FAILED_TERMINAL: "failed",
    TaskStatus.ABORTED_BUDGET: "aborted",
    TaskStatus.ABORTED_POLICY: "aborted",
}


def _fail(
    store: SQLiteStore,
    task: Task,
    settings: Settings,
    status: TaskStatus,
    event_payload: dict,
    error: str | None = None,
    cost_usd: float = 0.0,
    iterations: int = 0,
    duration: float = 0.0,
    work: IssueWork | None = None,
) -> None:
    store.append_task_event(task.id, "state_change", event_payload)
    store.update_status(
        task.id,
        status,
        error=error,
        cost_usd=cost_usd,
        iterations=iterations,
    )
    audit: dict = {
        "event": "task_processed",
        "task_id": task.id,
        "provider_instance": task.provider_instance,
        "repo": task.repo,
        "issue_number": task.issue_number,
        "trigger_kind": task.trigger_kind.value,
        "label_category": task.label_category,
        "outcome": status.value,
        "error": error,
        "cost_usd": cost_usd,
        "iterations": iterations,
    }
    # R8.2: entries for a task that is part of an issue_work item carry which
    # attempt and branch it ran on.
    if work is not None:
        audit["attempts"] = work.attempts
        audit["work_branch"] = work.work_branch
    write_audit_entry(settings.data_dir, audit)
    if cost_usd > 0:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        store.add_daily_budget(today, task.target_repo, cost_usd)
    label = _PROMETHEUS_LABEL.get(status, "failed")
    observe_task_result(
        label, duration, cost_usd, iterations, task.target_repo,
        provider_instance=task.provider_instance,
    )


def run_task(
    store: SQLiteStore, task: Task, settings: Settings, plugin: ProviderPlugin
) -> None:
    _start = time.monotonic()

    task_log = bind_context(
        logger,
        task_id=task.id,
        issue_number=task.issue_number,
        repo=task.repo,
    )

    route = settings.route_for(task.provider_instance, task.repo)
    if route is None:
        error = (
            f"no repo_routes entry for issue repo {task.repo!r} — the config "
            "must have changed between task enqueue and execution"
        )
        task_log.error("route_not_found", error=error)
        _fail(
            store, task, settings, TaskStatus.FAILED_RECOVERABLE,
            {"from": "running", "to": "failed_recoverable", "step": "route_resolution", "error": error},
            error=error, duration=time.monotonic() - _start,
        )
        return

    # A forge with no PR API delivers the pushed branch and a compare link
    # instead of a PR (PRD §3.7); `out.pulls is None` picks that path.
    out = _Outbound.for_task(plugin, task)
    publishes_branch = out.pulls is None

    issue_title = _extract_issue_title(task)

    # R7.6: the issue was closed (webhook or reconciliation) while this task
    # sat in the queue. Abort without cost — no clone, no agent, no comment
    # (commenting on a closed issue is noise). The row stays closed; a fresh
    # trigger on a reopened issue starts new work explicitly.
    prior = store.get_issue_work(task.provider_instance, task.repo, task.issue_number)
    if prior is not None and prior.state == "closed" and prior.closed_reason == "issue_closed":
        task_log.info("issue_closed_before_run")
        _fail(
            store, task, settings, TaskStatus.ABORTED_POLICY,
            {"from": "running", "to": "aborted_policy", "reason": "issue_closed"},
            error="issue_closed", duration=time.monotonic() - _start,
        )
        return

    # Per-issue work state (R4.1). The work branch is frozen on this
    # row the first time the issue is worked — computed from task.workflow
    # (frozen at webhook-ingestion time, NOT route.workflow which reflects
    # the current config) and the title in effect then — and read back on
    # every later run, so editing the issue title between attempts can't
    # silently move the branch (C4).
    work, resumed, force_continue = _open_or_resume_issue_work(
        store, task, settings, route, issue_title
    )
    branch = work.work_branch
    # On a resume, the prior attempt's handoff note rides into the next run's
    # prompt (R3.1 item 4, R5.6). A delivered reopen (`force_continue`)
    # gets this too, regardless of `resume.enabled` — it's a distinct
    # feature from the block-retry resume machinery that flag gates.
    resume_summary = (
        work.last_summary if resumed and (settings.resume_enabled or force_continue) else None
    )

    if settings.comments_initial_comment:
        try:
            out.comment(
                _build_initial_comment(
                    task, work, settings.agent_name,
                    pr_number=work.pr_number if force_continue else None,
                    published_branch=force_continue and publishes_branch,
                ),
            )
        except Exception:
            task_log.warning("initial_comment_failed")

    ws = Workspace(task.id, task.target_repo, settings, out.transport)

    try:
        ws_path = ws.clone(target_branch=task.base_branch)
    except Exception as exc:
        task_log.exception("clone_failed")
        try:
            default_branch = plugin.client(task.target_repo).get_default_branch()
            diag = (
                f"base branch '{task.base_branch}' not found in "
                f"{task.target_repo}; the repo's default branch is '{default_branch}' "
                f"— fix base_branch in this pair's configuration"
            )
        except Exception:
            diag = str(exc)
        _fail(
            store, task, settings, TaskStatus.FAILED_RECOVERABLE,
            {"from": "running", "to": "failed_recoverable", "step": "clone", "error": diag},
            error=diag, duration=time.monotonic() - _start, work=work,
        )
        return

    toolchain_env = _prepare_toolchain(ws_path, settings, route, task_log)

    try:
        # A resume clones the base (so `_salvage_agent_commits` keeps its
        # `origin/<base_branch>` ref, R4.2) and then checks out the work
        # branch's tip when it exists on the remote — otherwise it starts
        # fresh from the base, exactly like a first run.
        checked_out_wip = False
        base_update: BaseUpdate | None = None
        if resumed and (settings.resume_enabled or force_continue):
            git_env = out.git_env()
            if _remote_branch_exists(ws_path, branch, git_env):
                # Deepen the shallow base clone so the work branch and
                # `origin/<base_branch>` share history — `_salvage_agent_commits`
                # walks `origin/<base>..HEAD` (R4.2). Best-effort: a no-op /
                # error when the clone is already complete.
                with contextlib.suppress(Exception):
                    _run_git(ws_path, "fetch", "--unshallow", "origin", env=git_env)
                _run_git(ws_path, "fetch", "origin", branch, env=git_env)
                _run_git(ws_path, "checkout", "-b", branch, "FETCH_HEAD")
                # Bring the branch up to date with the base in the harness, so
                # the agent never has to (and never rewrites history to). A
                # conflict stays in progress for the agent to resolve.
                base_update = _update_branch_with_base(ws_path, task.base_branch, task_log)
                checked_out_wip = True
                task_log.info("resumed_from_work_branch", branch=branch)
        if not checked_out_wip:
            _run_git(ws_path, "checkout", "-b", branch)
        # Where the agent starts from; the salvage resets back to here. With a
        # base merge in progress HEAD is still the branch's old tip.
        start_sha = _head_sha(ws_path)
        # Moves to the merge commit once `_finish_base_merge` records it, so
        # the self-commit salvage never folds that merge back into one parent.
        salvage_sha = start_sha
        merge_pending = base_update is not None and base_update.conflicted
    except Exception as exc:
        task_log.exception("branch_failed")
        _fail(
            store, task, settings, TaskStatus.FAILED_RECOVERABLE,
            {"from": "running", "to": "failed_recoverable", "step": "branch", "error": str(exc)},
            error=str(exc), duration=time.monotonic() - _start, work=work,
        )
        ws.cleanup()
        return

    runner = _resolve_agent_runner(settings)
    on_progress = (
        _make_progress_reporter(out, task, settings.agent_name, task_log)
        if settings.comments_progress_every_iterations > 0
        else None
    )

    # Bounded, budget-respecting loop: a normal run makes exactly one pass
    # (`break` the first time the test verification below passes, is absent,
    # or the retry feature is off — byte-for-byte today's behavior). It only
    # loops again when the harness's own post-push test command fails and
    # `_decide_test_retry` says there's room left in `resume_max_attempts`
    # and the daily budget — reusing those existing caps rather than adding
    # a parallel one. `accumulated_cost`/`accumulated_iterations` track this
    # task's total spend across every loop iteration; `result` is reassigned
    # to carry that total once the loop ends, so every line below the loop
    # (PR creation, success bookkeeping) needs no further change.
    accumulated_cost = 0.0
    accumulated_iterations = 0
    current_resume_summary = resume_summary
    changed_files: list[tuple[str, str]] = []
    test_command: str | None = None
    test_passed: bool | None = None
    reported_checks: list[ReportedCheck] = []
    first_loop_iteration = True
    carried_over_commits = 0
    pushed_sha = ""

    branch_status = (
        build_branch_status(
            base_update.status, task.base_branch, out.capabilities.terms,
            base_update.conflicted_files,
            pr_number=work.pr_number if force_continue else None,
            published_branch=force_continue and publishes_branch,
        )
        if base_update is not None
        else ""
    )

    # Fetched once: a test retry continues the same conversation.
    comment_history = _fetch_comment_history(task, settings, out.forge)

    while True:
        try:
            result = runner.run(
                str(ws_path), task, settings, route,
                resume_summary=current_resume_summary, on_progress=on_progress,
                toolchain_env=toolchain_env,
                # Only the first pass starts on the harness's merge; a test
                # retry continues from what this run already pushed.
                branch_status=branch_status if first_loop_iteration else "",
                comment_history=comment_history,
                terms=out.capabilities.terms,
            )
        except Exception as exc:
            # A runner is supposed to return a failed AgentResult, but a
            # crash must still reach the salvage path below — never skip the
            # WIP push on the way out.
            task_log.exception("runner_raised")
            result = AgentResult(
                success=False, error="agent_crashed", error_detail=_error_detail(exc)
            )

        # Must run right after the agent's own run ends, before ANY git
        # operation touches the workspace — including `_preserve_wip_on_block`'s
        # `git add -A` on the block path below. The agent may have written the
        # `.agent-test-command` sentinel into the workspace root (which is the
        # git repo root) during its own run. Left in place, a block's WIP
        # commit would pick it up as an untracked file and push it into the
        # target repo's branch as a stray dotfile; reading it now and deleting
        # it immediately keeps it out of every git operation that follows, on
        # every exit path.
        sentinel_command = _consume_test_command_sentinel(ws_path)
        # Same lifecycle; a test retry re-runs the agent, so the footer
        # reports the last pass's checks.
        reported_checks = _consume_checks_sentinel(ws_path)

        if not result.success:
            task_log.warning(
                "agent_failed",
                error=result.error,
                cost=result.cost_usd,
                iters=result.iterations,
            )
            task_duration = time.monotonic() - _start
            # accumulated_* here is everything BEFORE this iteration (the
            # increment below hasn't run yet on this path) — add this
            # iteration's own spend for the task-row/issue_work/daily-budget
            # ledgers. Comments/alerts keep using `result.cost_usd` alone
            # (this run's own spend), matching how a separately resumed
            # Task's comment would read.
            task_total_cost = accumulated_cost + result.cost_usd
            task_total_iterations = accumulated_iterations + result.iterations

            if result.error in _SALVAGE_ERRORS:
                # A block (or an unexpected failure, e.g. the LLM provider
                # dropping the connection): preserve the partial work on the
                # branch and keep `issue_work` open so the next trigger
                # resumes it (R4.3). `_preserve_wip_on_block` may instead
                # close it — `no_changes` when there is nothing to resume
                # (R4.5), `max_attempts` when the retry ceiling is hit (R4.7).
                block_persist_result = (
                    result if accumulated_cost == 0.0 and accumulated_iterations == 0
                    else replace(result, cost_usd=task_total_cost, iterations=task_total_iterations)
                )
                # The WIP commit must not conclude a half-resolved base merge
                # (markers and all): record it if the agent got through every
                # conflict, back out of it otherwise.
                if merge_pending:
                    merge_pending = False
                    try:
                        unresolved = _finish_base_merge(
                            ws_path, branch, task.base_branch, start_sha, base_update,
                            task_log,
                        )
                    except Exception:
                        task_log.exception("base_merge_commit_failed")
                        unresolved = ["<merge commit failed>"]
                    if unresolved:
                        _abandon_base_merge(ws_path, start_sha)
                wip = _preserve_wip_on_block(
                    store, task, settings, work, ws_path, branch, block_persist_result,
                    out, task_log, start_sha,
                )
                ws.cleanup()
                if result.error not in _BLOCK_ERRORS:
                    _finish_salvaged_failure(
                        store, task, settings, out, work, wip, result,
                        task_total_cost, task_total_iterations, task_duration,
                        task_log,
                    )
                    return
                _fail(
                    store, task, settings, TaskStatus.ABORTED_BUDGET,
                    {"from": "running", "to": "aborted_budget", "reason": result.error},
                    error=result.error, cost_usd=task_total_cost, iterations=task_total_iterations,
                    duration=task_duration, work=work,
                )
                if result.error == "iteration_limit_exceeded":
                    alert_metric, alert_spent, alert_limit = (
                        "iterations", result.iterations, settings.max_iterations,
                    )
                else:
                    alert_metric, alert_spent, alert_limit = (
                        "cost", result.cost_usd, settings.max_budget_per_task_usd,
                    )
                send_budget_alert(
                    settings.alert_webhook_url,
                    result.error,
                    spent_usd=alert_spent,
                    limit_usd=alert_limit,
                    metric=alert_metric,
                    task_id=task.id,
                    issue_number=task.issue_number,
                    repo=task.repo,
                )
                try:
                    comment = _build_block_comment(
                        task, result, settings.agent_name, wip,
                        settings.resume_max_attempts, work,
                    )
                    out.comment(comment)
                except Exception:
                    task_log.exception("abort_comment_failed")
                return

            ws.cleanup()
            _fail(
                store, task, settings, TaskStatus.FAILED_TERMINAL,
                {"from": "running", "to": "failed_terminal", "error": result.error},
                error=result.error, cost_usd=task_total_cost, iterations=task_total_iterations,
                duration=task_duration, work=work,
            )
            _close_issue_work(
                store, task, "terminal_failure",
                total_cost_usd=work.total_cost_usd + task_total_cost,
                total_iterations=work.total_iterations + task_total_iterations,
            )
            try:
                comment = _build_abort_comment(task, result, settings.agent_name, work)
                out.comment(comment)
            except Exception:
                task_log.exception("abort_comment_failed")
            return

        accumulated_cost += result.cost_usd
        accumulated_iterations += result.iterations

        merged_base_now = False
        if merge_pending:
            merge_pending = False
            try:
                unresolved = _finish_base_merge(
                    ws_path, branch, task.base_branch, start_sha, base_update, task_log,
                )
            except Exception as exc:
                task_log.exception("base_merge_commit_failed")
                unresolved = [f"<merge commit failed: {exc}>"]
            if unresolved:
                _abandon_base_merge(ws_path, start_sha)
                _fail_unresolved_base_merge(
                    store, task, settings, out, work, force_continue,
                    unresolved, accumulated_cost, accumulated_iterations,
                    time.monotonic() - _start, task_log,
                )
                ws.cleanup()
                return
            merged_base_now = True
            salvage_sha = _head_sha(ws_path)

        try:
            status_output = _run_git(ws_path, "status", "--porcelain")
        except Exception as exc:
            task_log.exception("git_status_failed")
            _fail(
                store, task, settings, TaskStatus.FAILED_TERMINAL,
                {"from": "running", "to": "failed_terminal", "step": "git_status", "error": str(exc)},
                error=str(exc), duration=time.monotonic() - _start, work=work,
            )
            _close_issue_work(
                store, task, "terminal_failure",
                total_cost_usd=work.total_cost_usd + accumulated_cost,
                total_iterations=work.total_iterations + accumulated_iterations,
            )
            ws.cleanup()
            return

        if not status_output.strip():
            # A clean tree does not always mean "no work": the agent can
            # commit its own edits instead of leaving them staged — the
            # target repo's CLAUDE.md/AGENTS.md often tells it to. The
            # harness owns the commit and push (the agent's sandbox has no
            # push credential and no `gh`), so fold any commits it made on
            # top of the clone point back into the working tree and let the
            # normal flow below take over.
            status_output = _salvage_agent_commits(ws_path, branch, salvage_sha, task_log)

        # A resumed work branch can already carry the previous run's commits:
        # the agent then correctly writes nothing new, and the salvage above
        # legitimately finds nothing because `start_sha` IS that branch's
        # tip. The work is real and has never reached a PR, so it must not be
        # discarded as `no_changes` (issue #89). Pushing it is also what makes
        # `_update_branch_with_base`'s merge survive the workspace instead of
        # dying with the clone. A fresh branch is `origin/<base>` itself, so
        # this can only ever fire on a resume.
        carried_over_now = 0
        if (
            not status_output.strip() and first_loop_iteration and checked_out_wip
            and not merged_base_now
        ):
            ahead = _commits_ahead_of_base(ws_path, task.base_branch)
            # Content, not just commit count: a branch whose own commits were
            # squash-merged into the base is still "ahead" by the merge commit
            # alone, and must not open an empty PR.
            carried = (
                _changed_files_vs_base(ws_path, task.base_branch) if ahead else []
            )
            if ahead and carried:
                carried_over_now = ahead
                carried_over_commits = ahead
                changed_files = carried
                task_log.info(
                    "resumed_branch_already_ahead",
                    branch=branch,
                    commits_ahead=ahead,
                    base=task.base_branch,
                )

        # A recorded base merge is this run's work even with nothing else to
        # commit: it is what the follow-up asked for.
        if not status_output.strip() and not carried_over_now and not merged_base_now:
            if first_loop_iteration:
                task_log.info("no_changes")
                abt = AgentResult(
                    success=False, error="no_changes", cost_usd=accumulated_cost,
                    iterations=accumulated_iterations, submission=result.submission,
                )
                _fail(
                    store, task, settings, TaskStatus.ABORTED_POLICY,
                    {"from": "running", "to": "aborted_policy", "reason": "no_changes"},
                    error="no_changes", cost_usd=accumulated_cost, iterations=accumulated_iterations,
                    duration=time.monotonic() - _start, work=work,
                )
                summary = _resolve_agent_summary(result.submission)
                store.update_issue_work(
                    task.provider_instance, task.repo, task.issue_number,
                    last_summary=summary or "", last_summary_kind="submission" if summary else "",
                )
                _close_issue_work(
                    store, task, "no_changes",
                    total_cost_usd=work.total_cost_usd + accumulated_cost,
                    total_iterations=work.total_iterations + accumulated_iterations,
                )
                ws.cleanup()

                try:
                    comment = _build_abort_comment(task, abt, settings.agent_name, work)
                    out.comment(comment)
                except Exception:
                    task_log.exception("abort_comment_failed")
                return

            # A later retry iteration made no additional edits — the agent
            # believes the already-committed-and-pushed code is correct, but
            # the test still failed. There IS prior real work on this branch
            # (unlike the first-iteration case above), so don't abort the
            # whole issue_work: stop retrying and fall through to PR creation
            # with whatever the last test run showed, rather than spend
            # another attempt with no sign of progress.
            task_log.info("test_retry_no_additional_changes")
            break

        # On the carried-over path there is nothing to stage — the commits
        # are already on the branch and `changed_files` was read from the
        # diff against the base. Go straight to the integrity check and push.
        if not carried_over_now and status_output.strip():
            changed_files = _parse_git_status(status_output)

            commit_msg = pr_text.commit_message(
                out.issue_ref(), issue_title, _extract_author_login(task)
            )
            try:
                _run_git(ws_path, "add", "-A")
                _run_git(ws_path, "commit", "-m", commit_msg)
            except Exception as exc:
                task_log.exception("commit_failed")
                _fail(
                    store, task, settings, TaskStatus.FAILED_TERMINAL,
                    {"from": "running", "to": "failed_terminal", "step": "commit", "error": str(exc)},
                    error=str(exc), duration=time.monotonic() - _start, work=work,
                )
                _close_issue_work(
                    store, task, "terminal_failure",
                    total_cost_usd=work.total_cost_usd + accumulated_cost,
                    total_iterations=work.total_iterations + accumulated_iterations,
                )
                ws.cleanup()
                return

        if merged_base_now:
            # The PR's whole diff, read after the commit above: `git status`
            # would also have listed every path the base merge brought in.
            changed_files = _changed_files_vs_base(ws_path, task.base_branch)

        try:
            _assert_branch_integrity(ws_path, task.base_branch, start_sha)
        except BranchIntegrityError as exc:
            task_log.error("branch_integrity_failed", error=str(exc))
            _fail(
                store, task, settings, TaskStatus.ABORTED_POLICY,
                {"from": "running", "to": "aborted_policy", "reason": "branch_integrity"},
                error=str(exc), cost_usd=accumulated_cost, iterations=accumulated_iterations,
                duration=time.monotonic() - _start, work=work,
            )
            _close_issue_work(
                store, task, "branch_integrity",
                total_cost_usd=work.total_cost_usd + accumulated_cost,
                total_iterations=work.total_iterations + accumulated_iterations,
            )
            ws.cleanup()
            with contextlib.suppress(Exception):
                out.comment(
                    "Aborted before pushing: the branch would have reverted content "
                    f"already on `{task.base_branch}` ({exc}). Nothing was pushed; "
                    "please review manually.",
                )
            return

        try:
            # origin is already the tokenless URL Workspace.clone() set up (A2);
            # authenticate this push via env only, never by rewriting the remote
            # URL — that would put the credential back into .git/config.
            _run_git(ws_path, "push", "-f", "origin", branch, env=out.git_env())
            pushed_sha = _head_sha(ws_path)
        except Exception as exc:
            task_log.exception("push_failed")
            _fail(
                store, task, settings, TaskStatus.FAILED_RECOVERABLE,
                {"from": "running", "to": "failed_recoverable", "step": "push", "error": str(exc)},
                error=str(exc), duration=time.monotonic() - _start, work=work,
            )
            ws.cleanup()
            return

        test_command = _resolve_test_command(route.project, sentinel_command)
        test_passed = None
        test_output_excerpt = ""
        if test_command:
            test_passed, test_output_excerpt = _run_test_command(
                ws_path, test_command, settings.command_timeout_seconds, toolchain_env
            )
            task_log.info(
                "post_run_test_verification",
                command=test_command,
                passed=test_passed,
                output_excerpt=test_output_excerpt,
            )

        if test_passed is not False or not settings.resume_retry_on_test_failure:
            # Passed, no test configured, or the feature is off — done.
            break

        should_retry, stop_reason = _decide_test_retry(store, task, settings, work)
        if not should_retry:
            task_log.info("test_retry_stopped", reason=stop_reason)
            break

        work = _record_test_retry_attempt(store, task, work, test_command, test_output_excerpt)
        try:
            comment = _build_test_retry_comment(
                task, work, test_command, test_output_excerpt,
                settings.agent_name, settings.resume_max_attempts,
            )
            out.comment(comment)
        except Exception:
            task_log.warning("test_retry_comment_failed")

        current_resume_summary = _build_test_failure_resume_summary(test_command, test_output_excerpt)
        first_loop_iteration = False

    # Every remaining line below reads `result.cost_usd`/`result.iterations`
    # to mean "this task's total spend" — fold the loop's running totals in
    # once here so nothing past this point needs to change.
    result = replace(result, cost_usd=accumulated_cost, iterations=accumulated_iterations)
    testing_notes = _build_testing_notes(test_command, test_passed, reported_checks)
    pr_description = _resolve_pr_description(result.submission, issue_title)

    pr: PRRef | None = None
    if not publishes_branch:
        try:
            pr_title = pr_text.pr_title(out.issue_ref(), issue_title)
            if carried_over_commits:
                plural = "s" if carried_over_commits > 1 else ""
                pr_description = (
                    "> Opened by the harness: this run made no new edits. The branch "
                    f"already carried {carried_over_commits} commit{plural} ahead of "
                    f"`{task.base_branch}`.\n\n"
                    f"{pr_description}"
                )
            pr_body = pr_text.pr_body(
                issue_ref=out.issue_ref(),
                closing_reference=out.closing_reference(),
                task_id=task.id,
                description=pr_description,
                files_changed=changed_files,
                cost_usd=result.cost_usd,
                iterations=result.iterations,
                testing_notes=testing_notes,
                agent_name=settings.agent_name,
            )
            pr = out.open_pr(
                head=branch,
                base=task.base_branch,
                title=pr_title,
                body=pr_body,
                draft=route.draft,
                labels=route.labels,
            )
        except Exception as exc:
            task_log.exception("pr_failed")
            _fail(
                store, task, settings, TaskStatus.FAILED_RECOVERABLE,
                {"from": "running", "to": "failed_recoverable", "step": "pr", "error": str(exc)},
                error=str(exc), duration=time.monotonic() - _start, work=work,
            )
            ws.cleanup()
            return

    still_conflicting = False
    if checked_out_wip:
        # Only a resumed branch can lag behind its base; a branch freshly cut
        # from it cannot conflict.
        still_conflicting = _still_conflicting(
            out, pr, pushed_sha, ws_path, task.base_branch, task_log
        )
        if still_conflicting:
            task_log.warning(
                "branch_still_conflicting",
                pr=pr.number if pr else None, branch=branch, base=task.base_branch,
            )

    if pr is None:
        # A follow-up pushed onto the branch an earlier run published.
        updated_existing = force_continue
        if not updated_existing:
            try:
                out.label_issue([settings.pr_label_agent_generated, *route.labels])
            except Exception:
                task_log.warning("issue_label_failed")
        try:
            comment = _build_branch_published_comment(
                task, branch, out.compare_url(branch), result, pr_description,
                changed_files, testing_notes, settings.agent_name,
                out.capabilities.terms.pr, work,
                carried_over_commits=carried_over_commits,
                updated_existing=updated_existing, base_update=base_update,
                still_conflicting=still_conflicting,
            )
            out.comment(comment)
        except Exception:
            task_log.warning("issue_comment_failed")
    else:
        # A pr_opened reopen whose push landed on that same, still-open PR.
        # Its body stays as the original run wrote it (`open_pr` returns an
        # existing PR untouched); the update is reported in a PR comment.
        updated_existing = force_continue and pr.number == work.pr_number
        if updated_existing:
            try:
                out.comment_on_pr(
                    pr,
                    _build_pr_update_comment(
                        task, result, pr_description, changed_files, testing_notes,
                        settings.agent_name, base_update, still_conflicting,
                    ),
                )
            except Exception:
                task_log.warning("pr_comment_failed")

        try:
            comment = _build_fix_comment(
                task, pr, result, task.target_repo, settings.agent_name, work,
                carried_over_commits=carried_over_commits, branch=branch,
                updated_existing=updated_existing, still_conflicting=still_conflicting,
            )
            out.comment(comment)
        except Exception:
            task_log.warning("issue_comment_failed")

    task_duration = time.monotonic() - _start
    pr_number = pr.number if pr else None

    store.append_task_event(
        task.id, "state_change", {"from": "running", "to": "succeeded"},
    )
    store.update_status(
        task.id, TaskStatus.SUCCEEDED,
        pr_number=pr_number,
        cost_usd=result.cost_usd,
        iterations=result.iterations,
    )
    store.update_issue_work(
        task.provider_instance, task.repo, task.issue_number,
        last_summary=pr_description, last_summary_kind="submission",
    )
    _close_issue_work(
        store, task, out.delivery_reason, pr_number=pr_number,
        total_cost_usd=work.total_cost_usd + result.cost_usd,
        total_iterations=work.total_iterations + result.iterations,
    )
    if result.cost_usd > 0:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        store.add_daily_budget(today, task.target_repo, result.cost_usd)
    write_audit_entry(
        settings.data_dir,
        {
            "event": "task_processed",
            "task_id": task.id,
            "provider_instance": task.provider_instance,
            "repo": task.repo,
            "issue_number": task.issue_number,
            "trigger_kind": task.trigger_kind.value,
            "label_category": task.label_category,
            "outcome": "succeeded",
            "cost_usd": result.cost_usd,
            "iterations": result.iterations,
            "pr_number": pr_number,
            "attempts": work.attempts,
            "work_branch": work.work_branch,
        },
    )
    observe_task_result(
        "succeeded", task_duration, result.cost_usd, result.iterations, task.target_repo,
        provider_instance=task.provider_instance,
    )
    task_log.info(
        "task_succeeded",
        cost=result.cost_usd,
        iters=result.iterations,
        pr=pr_number,
    )

    ws.cleanup()
