from __future__ import annotations

from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.observability import prometheus_metrics as pm
from issue_agent.observability.prometheus_metrics import (
    budget_used_today_usd,
    init_metrics,
    llm_cost_usd_total,
    observe_task_result,
    queue_depth,
    set_budget_used,
    set_queue_depth,
    tasks_total,
)
from tests.helpers.settings import github_providers


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


init_metrics(
    SettingsNoEnv(
        PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
        LLM_API_KEY="k",
    )
)


def _counter_value(counter) -> float:
    return counter._value.get()


def _sum_value(histogram) -> float:
    return histogram._sum.get()


_REPO = "owner/repo-a"
_INSTANCE = "github"


class TestPrometheusMetrics:
    def test_observe_task_result_increments_counter(self) -> None:
        before = _counter_value(tasks_total.labels(status="succeeded", provider_instance=_INSTANCE, repo=_REPO))
        observe_task_result("succeeded", 10.0, 1.5, 5, _REPO, provider_instance=_INSTANCE)
        after = _counter_value(tasks_total.labels(status="succeeded", provider_instance=_INSTANCE, repo=_REPO))
        assert after == before + 1.0

    def test_observe_task_result_records_duration_histogram(self) -> None:
        before = _sum_value(pm.task_duration_seconds)
        observe_task_result("succeeded", 30.0, 0.0, 0, _REPO, provider_instance=_INSTANCE)
        after = _sum_value(pm.task_duration_seconds)
        assert after == before + 30.0

    def test_observe_task_result_records_cost(self) -> None:
        before = _counter_value(llm_cost_usd_total.labels(provider_instance=_INSTANCE, repo=_REPO))
        observe_task_result("succeeded", 0.0, 2.5, 0, _REPO, provider_instance=_INSTANCE)
        after = _counter_value(llm_cost_usd_total.labels(provider_instance=_INSTANCE, repo=_REPO))
        assert after == before + 2.5

    def test_observe_task_result_records_iterations(self) -> None:
        before = _sum_value(pm.iterations)
        observe_task_result("failed", 0.0, 0.0, 7, _REPO, provider_instance=_INSTANCE)
        after = _sum_value(pm.iterations)
        assert after == before + 7.0

    def test_observe_task_result_multiple_statuses(self) -> None:
        before_s = _counter_value(tasks_total.labels(status="succeeded", provider_instance=_INSTANCE, repo=_REPO))
        before_f = _counter_value(tasks_total.labels(status="failed", provider_instance=_INSTANCE, repo=_REPO))
        before_a = _counter_value(tasks_total.labels(status="aborted", provider_instance=_INSTANCE, repo=_REPO))

        observe_task_result("succeeded", 5.0, 1.0, 3, _REPO, provider_instance=_INSTANCE)
        observe_task_result("failed", 20.0, 0.5, 8, _REPO, provider_instance=_INSTANCE)
        observe_task_result("aborted", 0.0, 0.0, 0, _REPO, provider_instance=_INSTANCE)

        assert _counter_value(tasks_total.labels(status="succeeded", provider_instance=_INSTANCE, repo=_REPO)) == before_s + 1.0
        assert _counter_value(tasks_total.labels(status="failed", provider_instance=_INSTANCE, repo=_REPO)) == before_f + 1.0
        assert _counter_value(tasks_total.labels(status="aborted", provider_instance=_INSTANCE, repo=_REPO)) == before_a + 1.0

    def test_observe_task_result_labels_are_independent_per_repo(self) -> None:
        """Two repos recording results on the same status must not share a
        counter series — a regression that dropped the repo label (or
        collapsed it to a constant) would make repo-b's count include
        repo-a's increments too."""
        repo_a, repo_b = "owner/repo-a", "owner/repo-b"
        before_a = _counter_value(tasks_total.labels(status="succeeded", provider_instance=_INSTANCE, repo=repo_a))
        before_b = _counter_value(tasks_total.labels(status="succeeded", provider_instance=_INSTANCE, repo=repo_b))

        observe_task_result("succeeded", 1.0, 0.1, 1, repo_a, provider_instance=_INSTANCE)
        observe_task_result("succeeded", 1.0, 0.1, 1, repo_a, provider_instance=_INSTANCE)
        observe_task_result("succeeded", 1.0, 0.1, 1, repo_b, provider_instance=_INSTANCE)

        assert _counter_value(tasks_total.labels(status="succeeded", provider_instance=_INSTANCE, repo=repo_a)) == before_a + 2.0
        assert _counter_value(tasks_total.labels(status="succeeded", provider_instance=_INSTANCE, repo=repo_b)) == before_b + 1.0

    def test_the_same_repo_on_two_instances_gets_two_series(self) -> None:
        """owner/name is only unique within a forge: with two provider
        instances, the same repo name must not share a series (PRD Phase 4)."""
        def count(instance: str) -> float:
            return _counter_value(
                tasks_total.labels(status="succeeded", provider_instance=instance, repo=_REPO)
            )

        before_gh, before_ghe = count("github"), count("ghe")

        observe_task_result("succeeded", 1.0, 0.1, 1, _REPO, provider_instance="github")
        observe_task_result("succeeded", 1.0, 0.1, 1, _REPO, provider_instance="ghe")
        observe_task_result("succeeded", 1.0, 0.1, 1, _REPO, provider_instance="ghe")

        assert count("github") == before_gh + 1.0
        assert count("ghe") == before_ghe + 2.0

    def test_set_queue_depth(self) -> None:
        old = queue_depth._value.get()
        set_queue_depth(5)
        assert queue_depth._value.get() == 5.0
        set_queue_depth(old)

    def test_set_budget_used(self) -> None:
        gauge = budget_used_today_usd.labels(provider_instance=_INSTANCE, repo=_REPO)
        old = gauge._value.get()
        set_budget_used(25.50, _REPO, provider_instance=_INSTANCE)
        assert gauge._value.get() == 25.5
        set_budget_used(old, _REPO, provider_instance=_INSTANCE)

    def test_generate_latest_contains_all_metrics(self) -> None:
        from prometheus_client import generate_latest

        output = generate_latest().decode("utf-8")

        assert "issue_agent_tasks_total" in output
        assert "issue_agent_task_duration_seconds" in output
        assert "issue_agent_llm_cost_usd_total" in output
        assert "issue_agent_iterations" in output
        assert "issue_agent_queue_depth" in output
        assert "issue_agent_budget_used_today_usd" in output

    def test_observe_resume_increments_per_repo_counter(self) -> None:
        from issue_agent.observability.prometheus_metrics import (
            issue_work_resumes_total,
            observe_resume,
        )

        before = _counter_value(issue_work_resumes_total.labels(provider_instance=_INSTANCE, repo=_REPO))
        observe_resume(_REPO, provider_instance=_INSTANCE)
        observe_resume(_REPO, provider_instance=_INSTANCE)
        after = _counter_value(issue_work_resumes_total.labels(provider_instance=_INSTANCE, repo=_REPO))
        assert after == before + 2.0

    def test_observe_issue_work_closed_records_attempts_histogram(self) -> None:
        from issue_agent.observability.prometheus_metrics import (
            issue_work_attempts,
            observe_issue_work_closed,
        )

        before = _sum_value(issue_work_attempts.labels(provider_instance=_INSTANCE, repo=_REPO))
        observe_issue_work_closed(_REPO, 3, provider_instance=_INSTANCE)
        after = _sum_value(issue_work_attempts.labels(provider_instance=_INSTANCE, repo=_REPO))
        assert after == before + 3.0

    def test_fase_d_metrics_appear_in_exposition(self) -> None:
        from prometheus_client import generate_latest

        from issue_agent.observability.prometheus_metrics import (
            observe_issue_work_closed,
            observe_resume,
        )

        observe_resume(_REPO, provider_instance=_INSTANCE)
        observe_issue_work_closed(_REPO, 1, provider_instance=_INSTANCE)
        output = generate_latest().decode("utf-8")
        assert "issue_agent_issue_work_resumes_total" in output
        assert "issue_agent_issue_work_attempts" in output

    def test_init_metrics_is_idempotent(self) -> None:
        first_duration = pm.task_duration_seconds
        first_iterations = pm.iterations
        init_metrics(
            SettingsNoEnv(
                PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
                LLM_API_KEY="k",
            )
        )
        assert pm.task_duration_seconds is first_duration
        assert pm.iterations is first_iterations
