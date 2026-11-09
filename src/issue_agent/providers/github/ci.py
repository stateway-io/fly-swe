"""``CIClient`` for GitHub: check runs and commit statuses on a commit, and
the log of each failed one (GitHub Actions job logs; other apps' check
output). Needs the App's Checks, Commit statuses and Actions read access."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import requests

from issue_agent.core.models import CICheck, CIFailureLog, CIStatus
from issue_agent.providers.base import CIClient
from issue_agent.providers.github.client import GitHubClient

if TYPE_CHECKING:
    from github.CheckRun import CheckRun
    from github.Repository import Repository

    from issue_agent.providers.github.auth import GitHubAppAuth

# The app that owns GitHub Actions' check runs. For those, the check run's id
# is the Actions job's id, which is how its log is addressed.
_ACTIONS_APP_SLUG = "github-actions"

_CHECK_CONCLUSIONS: dict[str, Any] = {
    "success": "success",
    "neutral": "neutral",
    "skipped": "skipped",
    "failure": "failure",
    "timed_out": "failure",
    "cancelled": "failure",
    "action_required": "failure",
    "startup_failure": "failure",
    "stale": "failure",
}

_STATUS_STATES: dict[str, Any] = {
    "success": "success",
    "pending": "pending",
    "failure": "failure",
    "error": "failure",
}

# Actions prefixes every log line with an ISO timestamp — noise to the model.
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ", re.MULTILINE)

_LOG_TIMEOUT_SECONDS = 30


def _check_conclusion(run: CheckRun) -> Any:
    if run.status != "completed":
        return "pending"
    return _CHECK_CONCLUSIONS.get(run.conclusion or "", "failure")


def _check_ref(run: CheckRun) -> str:
    app = getattr(run, "app", None)
    slug = getattr(app, "slug", "") if app is not None else ""
    return f"{'actions' if slug == _ACTIONS_APP_SLUG else 'check'}:{run.id}"


def fold_ci_state(checks: list[CICheck]) -> CIStatus:
    """One verdict for every check on a commit — see ``CIState``."""
    if not checks:
        return CIStatus(state="none")
    conclusions = {c.conclusion for c in checks}
    if "pending" in conclusions:
        return CIStatus(state="pending", checks=checks)
    if "failure" in conclusions:
        return CIStatus(state="failure", checks=checks)
    return CIStatus(state="success", checks=checks)


def tail(text: str, max_chars: int) -> str:
    """The last ``max_chars`` of ``text`` — the end of a log is where the
    failure is reported."""
    text = _TIMESTAMP.sub("", text).strip()
    if max_chars <= 0:
        return ""
    return text if len(text) <= max_chars else text[-max_chars:]


class GitHubCIClient(CIClient):
    """``CIClient`` for one target repo."""

    def __init__(self, auth: GitHubAppAuth, repo: str) -> None:
        self._auth = auth
        self._repo_name = repo

    def _repo(self) -> Repository:
        client = GitHubClient(self._auth, self._auth.installation_for(self._repo_name))
        return client.get_repo(self._repo_name)

    def status(self, head_sha: str) -> CIStatus:
        commit = self._repo().get_commit(head_sha)
        checks = [
            CICheck(
                name=run.name,
                conclusion=_check_conclusion(run),
                url=run.html_url or run.details_url or "",
                ref_id=_check_ref(run),
            )
            for run in commit.get_check_runs()
        ]
        # Legacy commit statuses (external CI services); the combined status
        # already holds only the latest one per context.
        checks.extend(
            CICheck(
                name=status.context,
                conclusion=_STATUS_STATES.get(status.state, "failure"),
                url=status.target_url or "",
                ref_id=f"status:{status.context}",
                summary=status.description or "",
            )
            for status in commit.get_combined_status().statuses
        )
        return fold_ci_state(checks)

    def failure_logs(self, status: CIStatus, max_chars: int) -> list[CIFailureLog]:
        failed = status.failed
        if not failed:
            return []
        repo = self._repo()
        logs: list[CIFailureLog] = []
        for check in failed:
            kind, _, ref = check.ref_id.partition(":")
            try:
                if kind == "actions":
                    excerpt = self._actions_job_log(repo, int(ref))
                elif kind == "check":
                    excerpt = self._check_output(repo, int(ref))
                else:
                    # A commit status carries no log, only its description.
                    excerpt = check.summary
            except Exception:
                excerpt = ""
            logs.append(
                CIFailureLog(check_name=check.name, url=check.url, excerpt=tail(excerpt, max_chars))
            )
        return logs

    def _actions_job_log(self, repo: Repository, job_id: int) -> str:
        # The endpoint answers with a redirect to a short-lived, pre-signed
        # download URL that needs no credential.
        status, headers, body = repo.requester.requestJson(
            "GET", f"{repo.url}/actions/jobs/{job_id}/logs"
        )
        location = headers.get("location")
        if status in (301, 302, 307, 308) and location:
            response = requests.get(location, timeout=_LOG_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.text
        if status == 200:
            return body or ""
        raise RuntimeError(f"job log request answered {status}")

    def _check_output(self, repo: Repository, check_run_id: int) -> str:
        output = repo.get_check_run(check_run_id).output
        parts = [getattr(output, f, None) for f in ("title", "summary", "text")]
        return "\n\n".join(p for p in parts if p)
