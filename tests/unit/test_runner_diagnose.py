from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.diagnose.verdict import SENTINEL_FILENAME
from issue_agent.queue.models import Task, TaskStatus, TriggerKind
from issue_agent.queue.store import SQLiteStore
from issue_agent.runner.diagnose import run_diagnosis
from issue_agent.runner.executor import AgentResult
from tests.fakes.fake_provider import FakeProvider
from tests.helpers.factories import make_author, make_comment, make_issue, make_trigger_event
from tests.helpers.settings import github_providers


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8", env_prefix="", extra="ignore", yaml_file=None
    )


def _make_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
        LLM_API_KEY="k",
        DATA_DIR=str(tmp_path / "data"),
        REPO_ROUTES=[{"issues": "owner/issues-repo", "target": "owner/code-repo"}],
    )
    defaults.update(overrides)
    return SettingsNoEnv(**defaults)


def _make_task(**overrides) -> Task:
    defaults = dict(
        delivery_id="d1",
        repo="owner/issues-repo",
        issue_number=7,
        trigger_kind=TriggerKind.ISSUE_OPENED,
        label_category="bug",
        trigger_json=make_trigger_event(
            issue=make_issue(
                repo="owner/issues-repo", number=7, title="Broken thing", labels=["bug"]
            ),
            actor=make_author(login="reporter"),
        ).model_dump_json(),
        target_repo="owner/code-repo",
    )
    defaults.update(overrides)
    return Task(**defaults)


def _store() -> SQLiteStore:
    s = SQLiteStore(":memory:")
    s.init()
    return s


class _Patched:
    """Shared scaffolding: fakes every I/O boundary run_diagnosis touches
    (the forge, the git clone, and the LLM agent itself) so tests exercise
    only the orchestration in diagnose.py."""

    def __init__(self, tmp_path, agent_result: AgentResult, sentinel_content: str | None):
        self.tmp_path = tmp_path
        self.ws_path = tmp_path / "workspace"
        self.ws_path.mkdir()
        if sentinel_content is not None:
            (self.ws_path / SENTINEL_FILENAME).write_text(sentinel_content)

        self.plugin = FakeProvider("github", secret="s")
        self.mock_workspace = MagicMock()
        self.mock_workspace.clone.return_value = self.ws_path
        self.mock_runner_instance = MagicMock()
        self.mock_runner_instance.run.return_value = agent_result

        self._patches = [
            patch("issue_agent.runner.diagnose.Workspace", return_value=self.mock_workspace),
            patch(
                "issue_agent.runner.executor.MiniSWERunner",
                return_value=self.mock_runner_instance,
            ),
        ]

    def posted(self) -> list[str]:
        return self.plugin.forge.posted("owner/issues-repo", 7)

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()


def _audit_lines(settings: Settings) -> list[str]:
    import time
    from pathlib import Path

    today = time.strftime("%Y-%m-%d", time.gmtime())
    audit_path = Path(settings.data_dir) / f"audit.{today}.log"
    if not audit_path.exists():
        return []
    return audit_path.read_text().strip().split("\n")


class TestRunDiagnosisRouting:
    def test_route_not_found_fails_recoverable(self, tmp_path) -> None:
        settings = _make_settings(tmp_path)
        store = _store()
        task = _make_task(repo="owner/unrouted-repo")
        store.insert_task(task)
        fetched = store.fetch_next()

        run_diagnosis(store, fetched, settings, FakeProvider("github", secret="s"))

        final = store.get_task(task.id)
        assert final.status is TaskStatus.FAILED_RECOVERABLE


class TestRunDiagnosisCloneFailure:
    def test_clone_failure_fails_recoverable(self, tmp_path) -> None:
        settings = _make_settings(tmp_path)
        store = _store()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        with patch("issue_agent.runner.diagnose.Workspace") as mock_ws_cls:
            mock_ws_cls.return_value.clone.side_effect = RuntimeError("clone boom")
            run_diagnosis(store, fetched, settings, FakeProvider("github", secret="s"))

        final = store.get_task(task.id)
        assert final.status is TaskStatus.FAILED_RECOVERABLE
        assert "clone_failed" in (final.error or "")


class TestRunDiagnosisHappyPath:
    def test_matching_verdict_succeeds_and_comments(self, tmp_path) -> None:
        settings = _make_settings(tmp_path)
        store = _store()
        task = _make_task(label_category="bug")
        store.insert_task(task)
        fetched = store.fetch_next()

        sentinel = json.dumps(
            {"actual_category": "bug", "confidence": "high", "rationale": "Confirmed."}
        )
        agent_result = AgentResult(success=True, cost_usd=0.05, iterations=3, submission="done")

        with _Patched(tmp_path, agent_result, sentinel) as p:
            run_diagnosis(store, fetched, settings, p.plugin)
            [comment_body] = p.posted()

        final = store.get_task(task.id)
        assert final.status is TaskStatus.SUCCEEDED
        assert final.cost_usd == 0.05
        assert final.iterations == 3
        assert "bug" in comment_body

        assert (
            store.get_daily_budget(
                __import__("datetime")
                .datetime.now(__import__("datetime").UTC)
                .strftime("%Y-%m-%d"),
                "owner/code-repo",
            ).total_cost_usd
            == 0.05
        )

        audit = _audit_lines(settings)
        assert any(
            '"action": "diagnose"' in line and '"outcome": "succeeded"' in line for line in audit
        )

    def test_diverging_verdict_still_succeeds(self, tmp_path) -> None:
        settings = _make_settings(tmp_path)
        store = _store()
        task = _make_task(label_category="high_severity_bug")
        store.insert_task(task)
        fetched = store.fetch_next()

        sentinel = json.dumps(
            {"actual_category": "bug", "confidence": "medium", "rationale": "Edge case only."}
        )
        agent_result = AgentResult(success=True, cost_usd=0.10, iterations=5)

        with _Patched(tmp_path, agent_result, sentinel) as p:
            run_diagnosis(store, fetched, settings, p.plugin)
            [comment_body] = p.posted()

        final = store.get_task(task.id)
        assert final.status is TaskStatus.SUCCEEDED
        assert "high_severity_bug" in comment_body or "high severity" in comment_body.lower()

    def test_missing_verdict_posts_inconclusive_and_still_succeeds(self, tmp_path) -> None:
        settings = _make_settings(tmp_path)
        store = _store()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        agent_result = AgentResult(
            success=False, error="task_timeout", cost_usd=0.20, iterations=40
        )

        with _Patched(tmp_path, agent_result, sentinel_content=None) as p:
            run_diagnosis(store, fetched, settings, p.plugin)
            [comment_body] = p.posted()

        final = store.get_task(task.id)
        assert final.status is TaskStatus.SUCCEEDED
        assert "inconclusive" in comment_body.lower()

    def test_never_leaks_raw_agent_submission_into_comment(self, tmp_path) -> None:
        settings = _make_settings(tmp_path)
        store = _store()
        task = _make_task(label_category="bug")
        store.insert_task(task)
        fetched = store.fetch_next()

        sentinel = json.dumps(
            {"actual_category": "bug", "confidence": "high", "rationale": "Straightforward bug."}
        )
        agent_result = AgentResult(
            success=True,
            cost_usd=0.05,
            iterations=3,
            submission="SECRET_INTERNAL_TOKEN=abc123\ndef _leaked_helper(): pass",
        )

        with _Patched(tmp_path, agent_result, sentinel) as p:
            run_diagnosis(store, fetched, settings, p.plugin)
            [comment_body] = p.posted()

        assert "SECRET_INTERNAL_TOKEN" not in comment_body
        assert "_leaked_helper" not in comment_body

    def test_comment_failure_marks_audit_but_still_succeeds(self, tmp_path) -> None:
        settings = _make_settings(tmp_path)
        store = _store()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        sentinel = json.dumps({"actual_category": "bug", "confidence": "high", "rationale": ""})
        agent_result = AgentResult(success=True, cost_usd=0.01, iterations=1)

        with _Patched(tmp_path, agent_result, sentinel) as p:
            failing = MagicMock()
            failing.post_comment.side_effect = RuntimeError("forge down")
            with patch.object(p.plugin, "client", return_value=failing):
                run_diagnosis(store, fetched, settings, p.plugin)

        final = store.get_task(task.id)
        assert final.status is TaskStatus.SUCCEEDED

        audit = _audit_lines(settings)
        assert any('"outcome": "succeeded_with_comment_error"' in line for line in audit)

    def test_workspace_cleanup_always_called(self, tmp_path) -> None:
        settings = _make_settings(tmp_path)
        store = _store()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        sentinel = json.dumps({"actual_category": "bug", "confidence": "high", "rationale": ""})
        agent_result = AgentResult(success=True, cost_usd=0.0, iterations=1)

        with _Patched(tmp_path, agent_result, sentinel) as p:
            run_diagnosis(store, fetched, settings, p.plugin)
            p.mock_workspace.cleanup.assert_called_once()

    def test_zero_cost_does_not_touch_daily_budget(self, tmp_path) -> None:
        settings = _make_settings(tmp_path)
        store = _store()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        sentinel = json.dumps({"actual_category": "bug", "confidence": "high", "rationale": ""})
        agent_result = AgentResult(success=True, cost_usd=0.0, iterations=1)

        with _Patched(tmp_path, agent_result, sentinel) as p:
            run_diagnosis(store, fetched, settings, p.plugin)

        import datetime as _dt

        today = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d")
        assert store.get_daily_budget(today, "owner/code-repo").total_cost_usd == 0.0


class TestRunDiagnosisUsesTheProvider:
    def test_clones_the_target_with_the_plugin_transport_and_comments_on_the_issue(
        self, tmp_path
    ) -> None:
        settings = _make_settings(tmp_path)
        store = _store()
        task = _make_task()
        store.insert_task(task)
        fetched = store.fetch_next()

        sentinel = json.dumps({"actual_category": "bug", "confidence": "high", "rationale": ""})
        agent_result = AgentResult(success=True, cost_usd=0.0, iterations=1)

        with (
            _Patched(tmp_path, agent_result, sentinel) as p,
            patch("issue_agent.runner.diagnose.Workspace") as mock_ws_cls,
        ):
            mock_ws_cls.return_value = p.mock_workspace
            run_diagnosis(store, fetched, settings, p.plugin)

        args = mock_ws_cls.call_args[0]
        assert args[1] == "owner/code-repo"
        assert args[3].clone_url("owner/code-repo") == "https://forge.example/owner/code-repo.git"
        assert len(p.posted()) == 1
        assert p.plugin.forge.posted("owner/code-repo", 7) == []


class TestRunDiagnosisCommentHistory:
    def test_the_issue_thread_reaches_the_agent(self, tmp_path) -> None:
        settings = _make_settings(tmp_path)
        store = _store()
        trigger = make_trigger_event(
            TriggerKind.COMMENT_COMMAND,
            issue=make_issue(repo="owner/issues-repo", number=7, labels=["bug"]),
            trigger_comment=make_comment(author=make_author(login="reporter")),
        )
        task = _make_task(
            trigger_kind=TriggerKind.COMMENT_COMMAND, trigger_json=trigger.model_dump_json()
        )
        store.insert_task(task)
        fetched = store.fetch_next()
        sentinel = json.dumps({"actual_category": "bug", "confidence": "high", "rationale": ""})

        with _Patched(tmp_path, AgentResult(success=True), sentinel) as p:
            p.plugin.forge.comments[("owner/issues-repo", 7)] = [
                make_comment(id="1", body="still broken on main")
            ]
            run_diagnosis(store, fetched, settings, p.plugin)
            history = p.mock_runner_instance.run.call_args.kwargs["comment_history"]

        assert [c.body for c in history] == ["still broken on main"]

    def test_the_agent_is_prompted_in_the_forges_vocabulary(self, tmp_path) -> None:
        settings = _make_settings(tmp_path)
        store = _store()
        store.insert_task(_make_task())
        fetched = store.fetch_next()
        sentinel = json.dumps({"actual_category": "bug", "confidence": "high", "rationale": ""})

        with _Patched(tmp_path, AgentResult(success=True), sentinel) as p:
            run_diagnosis(store, fetched, settings, p.plugin)
            terms = p.mock_runner_instance.run.call_args.kwargs["terms"]

        assert terms == p.plugin.capabilities.terms
