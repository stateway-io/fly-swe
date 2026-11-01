from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.environments.local import LocalEnvironmentConfig
from minisweagent.models.litellm_model import LitellmModel, LitellmModelConfig

from issue_agent.logging import get_logger
from issue_agent.runner.environment import SandboxedLocalEnvironment
from issue_agent.runner.litellm_cost import CostAwareLitellmModel
from issue_agent.runner.prompts import INSTANCE_TEMPLATE, load_system_prompt, prompt_vars
from issue_agent.runner.templates import FORMAT_ERROR_TEMPLATE, build_observation_template

if TYPE_CHECKING:
    from issue_agent.config import LlmProfile, RepoRoute, Settings
    from issue_agent.providers.base import ProviderTerms
    from issue_agent.queue.models import Task

logger = get_logger(__name__)

# Called after an agent step with ``(iteration, cost_usd)`` — posts the
# "still working" comment. Injected by the executor, which owns the
# GitHub client.
ProgressCallback = Callable[[int, float], None]


class ProgressReportingAgent(DefaultAgent):
    """``DefaultAgent`` that posts a progress comment every
    ``progress_every_iterations`` steps, at most ``progress_max_per_run``
    times per run. The counter is per instance = per run.

    ``step()`` is the only override; the check is best-effort — a failed
    post is logged, still counts against the per-run cap (anti-spam), and
    never interrupts the run."""

    def __init__(
        self,
        *args,
        progress_every_iterations: int = 0,
        progress_max_per_run: int = 0,
        on_progress: ProgressCallback | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._progress_every = progress_every_iterations
        self._progress_max = progress_max_per_run
        self._on_progress = on_progress
        self._progress_reports_made = 0

    def step(self) -> list[dict]:
        result = super().step()
        self._maybe_report_progress()
        return result

    def _maybe_report_progress(self) -> None:
        if (
            self._progress_every <= 0
            or self._on_progress is None
            or self._progress_reports_made >= self._progress_max
        ):
            return
        if self.n_calls < (self._progress_reports_made + 1) * self._progress_every:
            return
        self._progress_reports_made += 1
        try:
            self._on_progress(self.n_calls, self.cost)
        except Exception:
            logger.warning("progress_comment_failed", iteration=self.n_calls)


def _is_openrouter_profile(profile: LlmProfile) -> bool:
    """True when the resolved profile targets OpenRouter — by explicit
    `provider` or by the model string's own `openrouter/...` prefix (how
    LiteLLM itself derives the provider when none is set)."""
    if profile.provider:
        return profile.provider == "openrouter"
    return profile.model.partition("/")[0] == "openrouter"


def _model_kwargs(profile: LlmProfile) -> dict:
    kwargs: dict = {"api_key": profile.api_key, "max_retries": profile.max_retries}
    if profile.provider:
        kwargs["custom_llm_provider"] = profile.provider
    if profile.base_url:
        kwargs["api_base"] = profile.base_url
    if profile.temperature is not None:
        kwargs["temperature"] = profile.temperature
    if profile.max_output_tokens is not None:
        kwargs["max_tokens"] = profile.max_output_tokens
    if profile.reasoning_effort is not None:
        # LiteLLM maps this to the provider's native shape — e.g. on
        # adaptive-thinking Anthropic models (Opus 4.6+, Sonnet 4.6+) it
        # becomes thinking={"type": "adaptive"} + output_config.effort; on
        # older models, a fixed thinking budget instead. A model with no
        # reasoning support raises UnsupportedParamsError (in
        # LitellmModel.abort_exceptions, so the task aborts rather than
        # retrying).
        kwargs["reasoning_effort"] = profile.reasoning_effort
        if _is_openrouter_profile(profile):
            # LiteLLM only allows reasoning_effort through for a model its
            # own static model_cost map flags supports_reasoning=true
            # (OpenrouterConfig.get_supported_openai_params ->
            # litellm.supports_reasoning()). That map has no openrouter/...
            # entries at all for most models (confirmed against litellm
            # 1.85.0 and the upstream main branch) — a newly released model
            # OpenRouter's own API already accepts the parameter for (per
            # https://openrouter.ai/docs/parameters) still raises a false
            # UnsupportedParamsError. OpenRouter's catalog is the source of
            # truth for what IT accepts, so force the param through for this
            # provider only; non-OpenRouter providers keep LiteLLM's
            # abort-on-unsupported protection intact.
            kwargs["allowed_openai_params"] = ["reasoning_effort"]
    if profile.fallback_model:
        fallback: dict = {
            "model": profile.fallback_model,
            # The fallback key is never part of a per-repo override (
            # RepoLlmOverride has no api_key field) — always the global pair.
            "api_key": profile.fallback_api_key or profile.api_key,
        }
        if profile.fallback_base_url:
            fallback["api_base"] = profile.fallback_base_url
        # LiteLLM retries this config if the primary model errors; the list is
        # stripped from the request before it reaches the provider API.
        kwargs["fallbacks"] = [fallback]
    return kwargs


def build_agent(
    task: Task,
    workspace_path: Path,
    settings: Settings,
    route: RepoRoute,
    *,
    terms: ProviderTerms,
    model: LitellmModel | None = None,
    on_progress: ProgressCallback | None = None,
    system_template: str | None = None,
    toolchain_env: dict[str, str] | None = None,
) -> DefaultAgent:
    if model is None:
        # Global `llm:` config with this repo's override (if any) applied
        # field by field — see RepoLlmOverride / Settings.llm_profile().
        profile = settings.llm_profile(route)
        if route.llm is not None:
            # Nothing else records which model ran a task (not the audit log,
            # not metrics) — once repos can diverge, this is the only trace.
            logger.info(
                "repo_llm_override_in_effect",
                repo=route.issues,
                model=profile.model,
            )
        # cost_tracking: strict (default) makes CostAwareLitellmModel raise
        # when a step's cost can't be determined — a masked 0.0 is how the
        # budget guards failed in production. lenient keeps the old
        # warn-and-carry-on behavior.
        cost_tracking = "default" if profile.cost_tracking == "strict" else "ignore_errors"
        set_cache_control = "default_end" if profile.prompt_caching == "default_end" else None
        model_kwargs = _model_kwargs(profile)
        if settings.llm_request_timeout_seconds > 0:
            model_kwargs["timeout"] = settings.llm_request_timeout_seconds
        model = CostAwareLitellmModel(
            config_class=LitellmModelConfig,
            model_name=profile.model,
            cost_tracking=cost_tracking,
            set_cache_control=set_cache_control,
            model_kwargs=model_kwargs,
            # The agent only checks its wall-time limit between steps; the
            # model bounds each LLM call (and its retries) by the same budget.
            wall_time_limit_seconds=settings.task_timeout_seconds,
            # LitellmModelConfig's defaults are the bare ones, not the ones
            # upstream ships in its YAML configs: an observation template with
            # no length cap, and "{{ error }}" as the whole format-error
            # message. Both are ported in runner/templates.py.
            observation_template=build_observation_template(
                profile.observation_max_chars, settings.budget_warn_at_percent
            ),
            format_error_template=FORMAT_ERROR_TEMPLATE,
        )

    env = SandboxedLocalEnvironment(
        config_class=LocalEnvironmentConfig,
        cwd=str(workspace_path),
        timeout=settings.command_timeout_seconds,
        env=toolchain_env or {},
    )

    # A progress-reporting subclass only when there is somewhere to
    # report to (on_progress) and reporting is enabled (threshold > 0);
    # otherwise the plain agent, unchanged.
    progress_on = on_progress is not None and settings.comments_progress_every_iterations > 0
    agent_cls = ProgressReportingAgent if progress_on else DefaultAgent
    extra_agent_kwargs: dict = (
        {
            "progress_every_iterations": settings.comments_progress_every_iterations,
            "progress_max_per_run": settings.comments_progress_max_per_run,
            "on_progress": on_progress,
        }
        if progress_on
        else {}
    )

    agent = agent_cls(
        model=model,
        env=env,
        config_class=AgentConfig,
        system_template=system_template
        or load_system_prompt(task.label_category, settings.prompt_category_map),
        # Static template — issue text is never embedded here. It is
        # passed in later as the `task` value via agent.run(task=...),
        # which MiniSWERunner does.
        instance_template=INSTANCE_TEMPLATE,
        step_limit=settings.max_iterations,
        cost_limit=settings.max_budget_per_task_usd,
        wall_time_limit_seconds=settings.task_timeout_seconds,
        output_path=Path(settings.data_dir) / "events" / task.id / "trajectory.json",
        **extra_agent_kwargs,
    )

    # Always set unconditionally: the prompt templates use Jinja's
    # StrictUndefined, which raises if any of these names is referenced but
    # missing — so every key must be present even when the value is an
    # empty string/list.
    agent.extra_template_vars = {
        "bot_name": settings.agent_name,
        "test_command": route.project.test_command,
        "lint_command": route.project.lint_command,
        "protected_paths": route.project.protected_paths,
        "relevant_extensions": route.project.relevant_extensions,
        # INSTANCE_TEMPLATE's <comments> block. Seeded empty here so
        # StrictUndefined is satisfied for every caller; MiniSWERunner
        # overwrites it with the filtered discussion thread before agent.run().
        "comments": "",
        # INSTANCE_TEMPLATE's <previous_work> block. Seeded empty;
        # MiniSWERunner fills it with issue_work.last_summary on a resume.
        "resume_summary": "",
        # INSTANCE_TEMPLATE's <branch_status> block. Seeded empty (a fresh
        # branch); MiniSWERunner fills it when the harness merged the base
        # into a resumed branch.
        "branch_status": "",
        # The forge's vocabulary ({{ terms.pr }}, {{ instructions_files }}),
        # so no prompt names a forge (PRD §3.2).
        **prompt_vars(terms),
    }

    return agent
