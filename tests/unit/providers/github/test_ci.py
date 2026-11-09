"""``GitHubCIClient``: folding check runs and commit statuses into one
verdict, and reading what each failed one left behind."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from issue_agent.core.models import CICheck, CIStatus
from issue_agent.providers.github.auth import GitHubAppAuth
from issue_agent.providers.github.ci import GitHubCIClient, fold_ci_state, tail

REPO = "owner/test-repo"


def _run(
    name: str,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    app: str = "github-actions",
    run_id: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        name=name,
        status=status,
        conclusion=conclusion,
        html_url=f"https://github.com/{REPO}/runs/{run_id}",
        details_url=None,
        app=SimpleNamespace(slug=app),
    )


def _status(context: str, state: str, description: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        context=context,
        state=state,
        target_url=f"https://ci.example/{context}",
        description=description,
    )


def _client(runs=(), statuses=()) -> tuple[GitHubCIClient, MagicMock]:
    auth = MagicMock(spec=GitHubAppAuth)
    auth.installation_for.return_value = 1
    repo = MagicMock()
    repo.url = f"https://api.github.com/repos/{REPO}"
    commit = repo.get_commit.return_value
    commit.get_check_runs.return_value = list(runs)
    commit.get_combined_status.return_value = SimpleNamespace(statuses=list(statuses))
    client = GitHubCIClient(auth, REPO)
    patcher = patch.object(GitHubCIClient, "_repo", return_value=repo)
    patcher.start()
    return client, repo


@pytest.fixture(autouse=True)
def _stop_patches():
    yield
    patch.stopall()


class TestFold:
    def test_no_checks_is_none(self):
        assert fold_ci_state([]) == CIStatus(state="none")

    def test_anything_running_is_pending_even_after_a_failure(self):
        checks = [
            CICheck(name="a", conclusion="failure"),
            CICheck(name="b", conclusion="pending"),
        ]
        assert fold_ci_state(checks).state == "pending"

    def test_a_finished_failure_is_failure(self):
        checks = [CICheck(name="a", conclusion="success"), CICheck(name="b", conclusion="failure")]
        assert fold_ci_state(checks).state == "failure"

    def test_neutral_and_skipped_do_not_fail(self):
        checks = [
            CICheck(name="a", conclusion="success"),
            CICheck(name="b", conclusion="skipped"),
            CICheck(name="c", conclusion="neutral"),
        ]
        assert fold_ci_state(checks).state == "success"


class TestStatus:
    def test_reads_the_commit_it_was_asked_about(self):
        client, repo = _client(runs=[_run("unit")])

        client.status("abc123")

        repo.get_commit.assert_called_once_with("abc123")

    def test_maps_check_run_conclusions(self):
        client, _ = _client(
            runs=[
                _run("ok", run_id=1),
                _run("slow", conclusion="timed_out", run_id=2),
                _run("odd", conclusion="something_new", run_id=3),
                _run("skip", conclusion="skipped", run_id=4),
            ]
        )

        status = client.status("sha")

        assert [c.conclusion for c in status.checks] == ["success", "failure", "failure", "skipped"]
        assert status.state == "failure"

    def test_an_unfinished_run_is_pending(self):
        client, _ = _client(runs=[_run("unit", status="in_progress", conclusion=None)])

        assert client.status("sha").state == "pending"

    def test_actions_and_other_apps_get_distinct_refs(self):
        client, _ = _client(runs=[_run("a", run_id=7), _run("b", app="circleci", run_id=8)])

        refs = [c.ref_id for c in client.status("sha").checks]

        assert refs == ["actions:7", "check:8"]

    def test_commit_statuses_count_too(self):
        client, _ = _client(
            runs=[_run("unit")],
            statuses=[_status("jenkins", "error", "Build broke"), _status("lint", "pending")],
        )

        status = client.status("sha")

        assert status.state == "pending"
        jenkins = status.checks[1]
        assert (jenkins.name, jenkins.conclusion, jenkins.summary) == (
            "jenkins",
            "failure",
            "Build broke",
        )
        assert jenkins.url == "https://ci.example/jenkins"

    def test_no_runs_and_no_statuses_is_none(self):
        client, _ = _client()

        assert client.status("sha").state == "none"


class TestFailureLogs:
    def test_nothing_failed_reads_nothing(self):
        client, repo = _client()

        assert client.failure_logs(CIStatus(state="success"), 100) == []
        repo.get_commit.assert_not_called()

    def test_actions_log_follows_the_redirect_and_keeps_the_tail(self):
        client, repo = _client()
        repo.requester.requestJson.return_value = (302, {"location": "https://blob/log"}, None)
        response = MagicMock(text="2026-01-01T00:00:00.1234567Z setup\n" + "x" * 50 + "\nFAILED")
        status = CIStatus(
            state="failure",
            checks=[CICheck(name="unit", conclusion="failure", ref_id="actions:42", url="u")],
        )

        with patch("issue_agent.providers.github.ci.requests.get", return_value=response) as get:
            [log] = client.failure_logs(status, max_chars=20)

        repo.requester.requestJson.assert_called_once_with(
            "GET", f"https://api.github.com/repos/{REPO}/actions/jobs/42/logs"
        )
        get.assert_called_once()
        assert get.call_args.args[0] == "https://blob/log"
        assert (log.check_name, log.url) == ("unit", "u")
        assert log.excerpt.endswith("FAILED")
        assert len(log.excerpt) == 20

    def test_actions_log_served_inline(self):
        client, repo = _client()
        repo.requester.requestJson.return_value = (200, {}, "boom")
        status = CIStatus(
            state="failure",
            checks=[CICheck(name="unit", conclusion="failure", ref_id="actions:1")],
        )

        [log] = client.failure_logs(status, max_chars=100)

        assert log.excerpt == "boom"

    def test_unreadable_log_yields_an_empty_excerpt(self):
        client, repo = _client()
        repo.requester.requestJson.return_value = (403, {}, None)
        status = CIStatus(
            state="failure",
            checks=[CICheck(name="unit", conclusion="failure", ref_id="actions:1")],
        )

        [log] = client.failure_logs(status, max_chars=100)

        assert log.excerpt == ""

    def test_other_apps_report_their_check_output(self):
        client, repo = _client()
        repo.get_check_run.return_value = SimpleNamespace(
            output=SimpleNamespace(title="3 failed", summary="see below", text=None)
        )
        status = CIStatus(
            state="failure",
            checks=[CICheck(name="circle", conclusion="failure", ref_id="check:9")],
        )

        [log] = client.failure_logs(status, max_chars=100)

        repo.get_check_run.assert_called_once_with(9)
        assert log.excerpt == "3 failed\n\nsee below"

    def test_commit_statuses_report_their_description(self):
        client, _ = _client()
        status = CIStatus(
            state="failure",
            checks=[
                CICheck(
                    name="jenkins",
                    conclusion="failure",
                    ref_id="status:jenkins",
                    summary="Build broke",
                )
            ],
        )

        [log] = client.failure_logs(status, max_chars=100)

        assert log.excerpt == "Build broke"

    def test_only_failed_checks_are_read(self):
        client, repo = _client()
        repo.requester.requestJson.return_value = (200, {}, "log")
        status = CIStatus(
            state="failure",
            checks=[
                CICheck(name="ok", conclusion="success", ref_id="actions:1"),
                CICheck(name="bad", conclusion="failure", ref_id="actions:2"),
            ],
        )

        logs = client.failure_logs(status, max_chars=100)

        assert [log.check_name for log in logs] == ["bad"]


class TestTail:
    def test_strips_actions_timestamps(self):
        assert tail("2026-11-09T10:00:00Z a\n2026-11-09T10:00:01.5Z b", 100) == "a\nb"

    def test_zero_budget_is_empty(self):
        assert tail("abc", 0) == ""


def test_repo_resolves_the_installation_per_call():
    auth = MagicMock(spec=GitHubAppAuth)
    auth.installation_for.return_value = 5
    client = GitHubCIClient(auth, REPO)

    with patch("issue_agent.providers.github.ci.GitHubClient") as gh:
        client._repo()

    auth.installation_for.assert_called_once_with(REPO)
    gh.assert_called_once_with(auth, 5)
    gh.return_value.get_repo.assert_called_once_with(REPO)
