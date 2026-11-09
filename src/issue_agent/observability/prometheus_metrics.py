from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import Counter, Gauge, Histogram, start_http_server

if TYPE_CHECKING:
    from issue_agent.config import Settings

tasks_total = Counter(
    "issue_agent_tasks_total",
    "Total tasks processed by status",
    ["status", "provider_instance", "repo"],
)

llm_cost_usd_total = Counter(
    "issue_agent_llm_cost_usd_total",
    "Total LLM cost in USD",
    ["provider_instance", "repo"],
)

queue_depth = Gauge(
    "issue_agent_queue_depth",
    "Current number of pending tasks in the queue",
)

budget_used_today_usd = Gauge(
    "issue_agent_budget_used_today_usd",
    "Total budget used today in USD",
    ["provider_instance", "repo"],
)

# Cardinality stays bounded: one series per repo, mirroring
# the existing repo-labelled metrics. Every repo-labelled
# metric also carries `provider_instance`: the same owner/name can live on two
# forges (PRD Phase 4).
issue_work_resumes_total = Counter(
    "issue_agent_issue_work_resumes_total",
    "Number of times an issue_work item was resumed (a later FIX run picked "
    "up an already-open work item)",
    ["provider_instance", "repo"],
)

issue_work_attempts = Histogram(
    "issue_agent_issue_work_attempts",
    "Attempts an issue_work item took before it closed",
    ["provider_instance", "repo"],
    buckets=[1, 2, 3, 4, 5, 10],
)

ci_monitor_outcomes_total = Counter(
    "issue_agent_ci_monitor_outcomes_total",
    "CI monitor verdicts on watched commits, by outcome (passed, "
    "fix_enqueued, exhausted, no_ci, timed_out, cancelled)",
    ["outcome", "provider_instance", "repo"],
)

# Buckets are configurable (config.yaml `metrics:`); these Histograms are
# created lazily by init_metrics() instead of at import time, since
# prometheus_client bakes the bucket boundaries in at construction.
task_duration_seconds: Histogram | None = None
iterations: Histogram | None = None

_DEFAULT_TASK_DURATION_BUCKETS = [1, 5, 10, 30, 60, 120, 300, 600]
_DEFAULT_ITERATION_BUCKETS = [1, 5, 10, 20, 50, 100]


def init_metrics(settings: Settings) -> None:
    """Create the config-dependent Histograms. Call once at process startup,
    before any observe_task_result() call."""
    global task_duration_seconds, iterations

    if task_duration_seconds is None:
        task_duration_seconds = Histogram(
            "issue_agent_task_duration_seconds",
            "Task execution duration in seconds",
            buckets=settings.metrics_task_duration_buckets,
        )
    if iterations is None:
        iterations = Histogram(
            "issue_agent_iterations",
            "Number of iterations per task",
            buckets=settings.metrics_iteration_buckets,
        )


def observe_task_result(
    status: str,
    duration: float,
    cost: float,
    iteration_count: int,
    repo: str,
    *,
    provider_instance: str,
) -> None:
    tasks_total.labels(status=status, provider_instance=provider_instance, repo=repo).inc()
    if task_duration_seconds is not None:
        task_duration_seconds.observe(duration)
    llm_cost_usd_total.labels(provider_instance=provider_instance, repo=repo).inc(cost)
    if iterations is not None:
        iterations.observe(iteration_count)


def observe_resume(repo: str, *, provider_instance: str) -> None:
    issue_work_resumes_total.labels(provider_instance=provider_instance, repo=repo).inc()


def observe_issue_work_closed(repo: str, attempts: int, *, provider_instance: str) -> None:
    issue_work_attempts.labels(provider_instance=provider_instance, repo=repo).observe(attempts)


def observe_ci_outcome(outcome: str, repo: str, *, provider_instance: str) -> None:
    ci_monitor_outcomes_total.labels(
        outcome=outcome, provider_instance=provider_instance, repo=repo
    ).inc()


def set_queue_depth(n: int) -> None:
    queue_depth.set(n)


def set_budget_used(cost: float, repo: str, *, provider_instance: str) -> None:
    budget_used_today_usd.labels(provider_instance=provider_instance, repo=repo).set(cost)


_metrics_server_started: set[int] = set()


def start_metrics_server(port: int) -> None:
    if port in _metrics_server_started:
        return
    start_http_server(port)
    _metrics_server_started.add(port)
