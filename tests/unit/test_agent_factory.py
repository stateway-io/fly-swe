from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from pydantic_settings import SettingsConfigDict

from issue_agent.config import ProjectProfile, RepoLlmOverride, RepoRoute, Settings
from issue_agent.queue.models import Task, TriggerKind
from issue_agent.runner.agent_factory import ProgressReportingAgent, build_agent
from issue_agent.runner.prompts import INSTANCE_TEMPLATE
from tests.helpers.github import GITHUB_TERMS as TERMS
from tests.helpers.settings import github_providers


class _SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


def _make_settings(**overrides) -> Settings:
    defaults = dict(
        PROVIDERS=github_providers(app_id="1", app_private_key="fake-key", webhook_secret="s"),
        LLM_API_KEY="k",
        LLM_BASE_URL="",
        REPO_ROUTES=[
            {
                "issues": "stateway-io/stateway-issues",
                "target": "stateway-io/stateway-agent-fixtures",
            }
        ],
    )
    defaults.update(overrides)
    return _SettingsNoEnv(**defaults)


def _make_route(llm: RepoLlmOverride | None = None, **project_overrides) -> RepoRoute:
    return RepoRoute(
        issues="stateway-io/stateway-issues",
        target="stateway-io/stateway-agent-fixtures",
        project=ProjectProfile(**project_overrides),
        llm=llm,
    )


def _make_task(label_category: str | None = "bug") -> Task:
    return Task(
        id="task-007",
        delivery_id="del-007",
        repo="stateway-io/stateway-issues",
        issue_number=42,
        trigger_kind=TriggerKind.ISSUE_OPENED,
        label_category=label_category,
    )


class TestBuildAgent:
    def test_creates_litellm_model_with_correct_config(self) -> None:
        settings = _make_settings(
            LLM_MODEL="anthropic/claude-sonnet-4-7",
            LLM_MAX_RETRIES=3,
        )
        task = _make_task()

        with (
            patch("issue_agent.runner.agent_factory.CostAwareLitellmModel") as mock_model_cls,
            patch("issue_agent.runner.agent_factory.SandboxedLocalEnvironment") as mock_env_cls,
            patch("issue_agent.runner.agent_factory.DefaultAgent") as mock_agent_cls,
            patch("issue_agent.runner.agent_factory.load_system_prompt") as mock_load,
        ):
            mock_model_cls.return_value = MagicMock()
            mock_env_cls.return_value = MagicMock()
            mock_agent_cls.return_value = MagicMock()
            mock_load.return_value = "system prompt"

            build_agent(task, Path("/ws/stateway"), settings, _make_route(), terms=TERMS)

        mock_model_cls.assert_called_once()
        kwargs = mock_model_cls.call_args[1]
        assert kwargs["model_name"] == "anthropic/claude-sonnet-4-7"
        assert kwargs["model_kwargs"] == {
            "api_key": "k",
            "max_retries": 3,
            "timeout": 120,
        }
        # LitellmModelConfig's own defaults cap nothing and reduce a format
        # error to "{{ error }}"; the YAML-equivalent templates must be passed
        # explicitly (runner/templates.py).
        assert "<output_head>" in kwargs["observation_template"]
        assert "bash" in kwargs["format_error_template"]
        # cost_tracking defaults to strict -> mini-swe-agent's "default" mode
        assert kwargs["cost_tracking"] == "default"
        # prompt_caching defaults to default_end
        assert kwargs["set_cache_control"] == "default_end"


class TestProgressReportingAgent:
    """A DefaultAgent subclass that posts a progress comment
    every N iterations, capped per run, best-effort."""

    def _agent(self, on_progress, every: int = 20, mx: int = 2) -> ProgressReportingAgent:
        return ProgressReportingAgent(
            model=MagicMock(),
            env=MagicMock(),
            system_template="s",
            instance_template="i",
            progress_every_iterations=every,
            progress_max_per_run=mx,
            on_progress=on_progress,
        )

    def test_reports_only_once_the_threshold_is_crossed(self) -> None:
        calls: list[tuple[int, float]] = []
        agent = self._agent(lambda i, c: calls.append((i, c)))
        agent.cost = 0.4

        agent.n_calls = 19
        agent._maybe_report_progress()
        assert calls == []

        agent.n_calls = 20
        agent._maybe_report_progress()
        assert calls == [(20, 0.4)]

    def test_caps_at_max_per_run(self) -> None:
        calls: list[int] = []
        agent = self._agent(lambda i, c: calls.append(i), mx=2)
        for n in (20, 40, 60, 80):
            agent.n_calls = n
            agent._maybe_report_progress()
        assert calls == [20, 40]

    def test_zero_threshold_disables_reporting(self) -> None:
        calls: list[int] = []
        agent = self._agent(lambda i, c: calls.append(i), every=0)
        agent.n_calls = 100
        agent._maybe_report_progress()
        assert calls == []

    def test_callback_failure_never_propagates(self) -> None:
        def boom(i: int, c: float) -> None:
            raise RuntimeError("github is down")

        agent = self._agent(boom)
        agent.n_calls = 20
        agent._maybe_report_progress()  # must not raise
        # a failed post still counts against the per-run cap (anti-spam)
        assert agent._progress_reports_made == 1

    def test_step_runs_the_progress_check(self) -> None:
        agent = self._agent(lambda i, c: None)
        with (
            patch("minisweagent.agents.default.DefaultAgent.step", return_value=[]) as mock_super,
            patch.object(ProgressReportingAgent, "_maybe_report_progress") as mock_check,
        ):
            agent.step()
        mock_super.assert_called_once()
        mock_check.assert_called_once()


class TestModelKwargsAndTracking:
    def _build(self, settings, route=None):
        route = route or _make_route()
        with (
            patch("issue_agent.runner.agent_factory.CostAwareLitellmModel") as mock_model_cls,
            patch("issue_agent.runner.agent_factory.SandboxedLocalEnvironment"),
            patch("issue_agent.runner.agent_factory.DefaultAgent"),
            patch("issue_agent.runner.agent_factory.load_system_prompt", return_value="p"),
        ):
            build_agent(_make_task(), Path("/ws"), settings, route, terms=TERMS)
        return mock_model_cls.call_args[1]

    def test_lenient_cost_tracking_maps_to_ignore_errors(self):
        kwargs = self._build(_make_settings(LLM_COST_TRACKING="lenient"))
        assert kwargs["cost_tracking"] == "ignore_errors"

    def test_prompt_caching_none_disables_cache_control(self):
        kwargs = self._build(_make_settings(LLM_PROMPT_CACHING="none"))
        assert kwargs["set_cache_control"] is None

    def test_observation_template_carries_the_configured_cap(self):
        kwargs = self._build(_make_settings(LLM_OBSERVATION_MAX_CHARS=400))
        assert "output.output | length < 400" in kwargs["observation_template"]
        assert "output.output[:200]" in kwargs["observation_template"]

    def test_observation_cap_can_be_disabled(self):
        kwargs = self._build(_make_settings(LLM_OBSERVATION_MAX_CHARS=0))
        assert "<output_head>" not in kwargs["observation_template"]

    def test_budget_status_threshold_comes_from_settings(self):
        kwargs = self._build(_make_settings(BUDGET_WARN_AT_PERCENT=50))
        assert "<budget_status>" in kwargs["observation_template"]
        assert "step_limit * 0.5000" in kwargs["observation_template"]

    def test_budget_status_can_be_disabled(self):
        kwargs = self._build(_make_settings(BUDGET_WARN_AT_PERCENT=0))
        assert "<budget_status>" not in kwargs["observation_template"]

    def test_explicit_provider_passed_as_custom_llm_provider(self):
        kwargs = self._build(_make_settings(LLM_PROVIDER="openrouter"))
        assert kwargs["model_kwargs"]["custom_llm_provider"] == "openrouter"

    def test_sampling_params_passed_only_when_set(self):
        kwargs = self._build(_make_settings(LLM_TEMPERATURE="0.3", LLM_MAX_OUTPUT_TOKENS="1234"))
        assert kwargs["model_kwargs"]["temperature"] == 0.3
        assert kwargs["model_kwargs"]["max_tokens"] == 1234

    def test_sampling_params_absent_by_default(self):
        kwargs = self._build(_make_settings())
        assert "temperature" not in kwargs["model_kwargs"]
        assert "max_tokens" not in kwargs["model_kwargs"]
        assert "reasoning_effort" not in kwargs["model_kwargs"]

    def test_reasoning_effort_passed_when_set(self):
        kwargs = self._build(_make_settings(LLM_REASONING_EFFORT="high"))
        assert kwargs["model_kwargs"]["reasoning_effort"] == "high"

    def test_reasoning_effort_forces_allowed_openai_params_on_openrouter(self):
        """LiteLLM's static supports_reasoning() map has no openrouter/...
        entry for most models (newly released ones especially — see
        agent_factory._is_openrouter_profile), so it raises a false
        UnsupportedParamsError even when OpenRouter's own API accepts the
        parameter. allowed_openai_params forces it through for this
        provider only."""
        kwargs = self._build(
            _make_settings(
                LLM_MODEL="openrouter/~deepseek/deepseek-v4-flash-latest",
                LLM_REASONING_EFFORT="high",
            )
        )
        assert kwargs["model_kwargs"]["allowed_openai_params"] == ["reasoning_effort"]

    def test_reasoning_effort_does_not_force_allowed_params_on_other_providers(self):
        kwargs = self._build(
            _make_settings(
                LLM_MODEL="anthropic/claude-sonnet-4-7",
                LLM_REASONING_EFFORT="high",
            )
        )
        assert "allowed_openai_params" not in kwargs["model_kwargs"]

    def test_reasoning_effort_detects_openrouter_via_explicit_provider(self):
        """Even when the model string itself has no openrouter/ prefix (e.g.
        a bare alias), an explicit llm.provider: openrouter still triggers
        the bypass."""
        kwargs = self._build(
            _make_settings(
                LLM_MODEL="~deepseek/deepseek-v4-flash-latest",
                LLM_PROVIDER="openrouter",
                LLM_REASONING_EFFORT="high",
            )
        )
        assert kwargs["model_kwargs"]["allowed_openai_params"] == ["reasoning_effort"]

    def test_allowed_openai_params_absent_when_reasoning_effort_unset(self):
        kwargs = self._build(
            _make_settings(LLM_MODEL="openrouter/~deepseek/deepseek-v4-flash-latest")
        )
        assert "allowed_openai_params" not in kwargs["model_kwargs"]

    def test_no_fallbacks_key_when_fallback_model_unset(self):
        kwargs = self._build(_make_settings())
        assert "fallbacks" not in kwargs["model_kwargs"]

    def test_fallback_built_with_dedicated_key(self):
        kwargs = self._build(
            _make_settings(
                LLM_FALLBACK_MODEL="openrouter/anthropic/claude-sonnet-4-5",
                LLM_FALLBACK_BASE_URL="https://proxy.example",
                LLM_FALLBACK_API_KEY="sk-fallback",
            )
        )
        assert kwargs["model_kwargs"]["fallbacks"] == [
            {
                "model": "openrouter/anthropic/claude-sonnet-4-5",
                "api_key": "sk-fallback",
                "api_base": "https://proxy.example",
            }
        ]

    def test_fallback_key_defaults_to_primary_key(self):
        kwargs = self._build(
            _make_settings(
                LLM_API_KEY="sk-primary",
                LLM_FALLBACK_MODEL="openrouter/x/y",
            )
        )
        assert kwargs["model_kwargs"]["fallbacks"] == [
            {"model": "openrouter/x/y", "api_key": "sk-primary"}
        ]

    def test_creates_sandboxed_local_environment_with_workspace_path(self) -> None:
        settings = _make_settings(COMMAND_TIMEOUT_SECONDS="99")
        task = _make_task()

        with (
            patch("issue_agent.runner.agent_factory.CostAwareLitellmModel") as mock_model_cls,
            patch("issue_agent.runner.agent_factory.SandboxedLocalEnvironment") as mock_env_cls,
            patch("issue_agent.runner.agent_factory.DefaultAgent") as mock_agent_cls,
            patch("issue_agent.runner.agent_factory.load_system_prompt") as mock_load,
        ):
            mock_model_cls.return_value = MagicMock()
            mock_env_cls.return_value = MagicMock()
            mock_agent_cls.return_value = MagicMock()
            mock_load.return_value = ""

            build_agent(
                task, Path("/workspaces/task-007/stateway"), settings, _make_route(), terms=TERMS
            )

        mock_env_cls.assert_called_once()
        kwargs = mock_env_cls.call_args[1]
        assert kwargs["cwd"] == "/workspaces/task-007/stateway"
        assert kwargs["timeout"] == 99

    def test_sets_step_and_cost_limits_in_agent_config(self) -> None:
        settings = _make_settings(
            MAX_ITERATIONS="20",
            MAX_BUDGET_PER_TASK_USD="1.50",
        )
        task = _make_task()

        with (
            patch("issue_agent.runner.agent_factory.CostAwareLitellmModel") as mock_model_cls,
            patch("issue_agent.runner.agent_factory.SandboxedLocalEnvironment") as mock_env_cls,
            patch("issue_agent.runner.agent_factory.DefaultAgent") as mock_agent_cls,
            patch("issue_agent.runner.agent_factory.load_system_prompt") as mock_load,
        ):
            mock_model_cls.return_value = MagicMock()
            mock_env_cls.return_value = MagicMock()
            mock_agent_cls.return_value = MagicMock()
            mock_load.return_value = ""

            build_agent(task, Path("/ws"), settings, _make_route(), terms=TERMS)

        mock_agent_cls.assert_called_once()
        kwargs = mock_agent_cls.call_args[1]
        assert kwargs["step_limit"] == 20
        assert kwargs["cost_limit"] == 1.50

    def test_sets_wall_time_limit_from_task_timeout_seconds(self) -> None:
        settings = _make_settings(TASK_TIMEOUT_SECONDS="600")
        task = _make_task()

        with (
            patch("issue_agent.runner.agent_factory.CostAwareLitellmModel") as mock_model_cls,
            patch("issue_agent.runner.agent_factory.SandboxedLocalEnvironment") as mock_env_cls,
            patch("issue_agent.runner.agent_factory.DefaultAgent") as mock_agent_cls,
            patch("issue_agent.runner.agent_factory.load_system_prompt") as mock_load,
        ):
            mock_model_cls.return_value = MagicMock()
            mock_env_cls.return_value = MagicMock()
            mock_agent_cls.return_value = MagicMock()
            mock_load.return_value = ""

            build_agent(task, Path("/ws"), settings, _make_route(), terms=TERMS)

        mock_agent_cls.assert_called_once()
        kwargs = mock_agent_cls.call_args[1]
        assert kwargs["wall_time_limit_seconds"] == 600

    def test_model_gets_the_same_wall_time_limit_to_bound_each_llm_call(self) -> None:
        settings = _make_settings(TASK_TIMEOUT_SECONDS="600")
        task = _make_task()

        with (
            patch("issue_agent.runner.agent_factory.CostAwareLitellmModel") as mock_model_cls,
            patch("issue_agent.runner.agent_factory.SandboxedLocalEnvironment") as mock_env_cls,
            patch("issue_agent.runner.agent_factory.DefaultAgent") as mock_agent_cls,
            patch("issue_agent.runner.agent_factory.load_system_prompt") as mock_load,
        ):
            mock_model_cls.return_value = MagicMock()
            mock_env_cls.return_value = MagicMock()
            mock_agent_cls.return_value = MagicMock()
            mock_load.return_value = ""

            build_agent(task, Path("/ws"), settings, _make_route(), terms=TERMS)

        assert mock_model_cls.call_args[1]["wall_time_limit_seconds"] == 600

    def test_sets_output_path_under_data_dir_events_task_id(self) -> None:
        settings = _make_settings(DATA_DIR="/tmp/agent-factory-data")
        task = _make_task()

        with (
            patch("issue_agent.runner.agent_factory.CostAwareLitellmModel") as mock_model_cls,
            patch("issue_agent.runner.agent_factory.SandboxedLocalEnvironment") as mock_env_cls,
            patch("issue_agent.runner.agent_factory.DefaultAgent") as mock_agent_cls,
            patch("issue_agent.runner.agent_factory.load_system_prompt") as mock_load,
        ):
            mock_model_cls.return_value = MagicMock()
            mock_env_cls.return_value = MagicMock()
            mock_agent_cls.return_value = MagicMock()
            mock_load.return_value = ""

            build_agent(task, Path("/ws"), settings, _make_route(), terms=TERMS)

        mock_agent_cls.assert_called_once()
        kwargs = mock_agent_cls.call_args[1]
        assert kwargs["output_path"] == Path(
            "/tmp/agent-factory-data/events/task-007/trajectory.json"
        )

    def test_loads_system_prompt_via_label_category(self) -> None:
        settings = _make_settings()
        task = _make_task(label_category="docs")

        with (
            patch("issue_agent.runner.agent_factory.CostAwareLitellmModel") as mock_model_cls,
            patch("issue_agent.runner.agent_factory.SandboxedLocalEnvironment") as mock_env_cls,
            patch("issue_agent.runner.agent_factory.DefaultAgent") as mock_agent_cls,
            patch("issue_agent.runner.agent_factory.load_system_prompt") as mock_load,
        ):
            mock_model_cls.return_value = MagicMock()
            mock_env_cls.return_value = MagicMock()
            mock_agent_cls.return_value = MagicMock()
            mock_load.return_value = "docs prompt"

            build_agent(task, Path("/ws"), settings, _make_route(), terms=TERMS)

        mock_load.assert_called_once_with("docs", settings.prompt_category_map)
        assert mock_agent_cls.call_args[1]["system_template"] == "docs prompt"

    def test_instance_template_is_the_static_task_template(self) -> None:
        """instance_template must never be issue-derived text — it is
        always the static INSTANCE_TEMPLATE ({{ task }}), regardless of the task.
        build_user_message() is no longer called here; MiniSWERunner passes the
        issue-derived message through agent.run(task=...) instead."""
        settings = _make_settings()
        task = _make_task()

        with (
            patch("issue_agent.runner.agent_factory.CostAwareLitellmModel") as mock_model_cls,
            patch("issue_agent.runner.agent_factory.SandboxedLocalEnvironment") as mock_env_cls,
            patch("issue_agent.runner.agent_factory.DefaultAgent") as mock_agent_cls,
            patch("issue_agent.runner.agent_factory.load_system_prompt") as mock_load,
        ):
            mock_model_cls.return_value = MagicMock()
            mock_env_cls.return_value = MagicMock()
            mock_agent_cls.return_value = MagicMock()
            mock_load.return_value = ""

            build_agent(task, Path("/ws"), settings, _make_route(), terms=TERMS)

        assert mock_agent_cls.call_args[1]["instance_template"] == INSTANCE_TEMPLATE
        assert "{{ task }}" in mock_agent_cls.call_args[1]["instance_template"]

    def test_returns_agent_instance(self) -> None:
        settings = _make_settings()
        task = _make_task()

        with (
            patch("issue_agent.runner.agent_factory.CostAwareLitellmModel") as mock_model_cls,
            patch("issue_agent.runner.agent_factory.SandboxedLocalEnvironment") as mock_env_cls,
            patch("issue_agent.runner.agent_factory.DefaultAgent") as mock_agent_cls,
            patch("issue_agent.runner.agent_factory.load_system_prompt") as mock_load,
        ):
            mock_model_cls.return_value = MagicMock()
            mock_env_cls.return_value = MagicMock()
            expected_agent = MagicMock()
            mock_agent_cls.return_value = expected_agent
            mock_load.return_value = ""

            result = build_agent(task, Path("/ws"), settings, _make_route(), terms=TERMS)

        assert result is expected_agent

    def test_template_vars_carry_the_forges_vocabulary(self) -> None:
        with (
            patch("issue_agent.runner.agent_factory.CostAwareLitellmModel"),
            patch("issue_agent.runner.agent_factory.SandboxedLocalEnvironment"),
            patch("issue_agent.runner.agent_factory.load_system_prompt", return_value="p"),
        ):
            agent = build_agent(
                _make_task(), Path("/ws"), _make_settings(), _make_route(), terms=TERMS
            )

        assert agent.extra_template_vars["terms"] == TERMS.model_dump()
        assert agent.extra_template_vars["instructions_files"] == (
            "`AGENTS.md`, `CLAUDE.md`, or `.github/copilot-instructions.md`"
        )

    def test_returns_progress_agent_when_on_progress_and_threshold_set(self) -> None:
        settings = _make_settings(COMMENTS_PROGRESS_EVERY_ITERATIONS="20")
        with (
            patch("issue_agent.runner.agent_factory.CostAwareLitellmModel"),
            patch("issue_agent.runner.agent_factory.SandboxedLocalEnvironment"),
            patch("issue_agent.runner.agent_factory.load_system_prompt", return_value="p"),
        ):
            agent = build_agent(
                _make_task(),
                Path("/ws"),
                settings,
                _make_route(),
                terms=TERMS,
                on_progress=lambda i, c: None,
            )
        assert isinstance(agent, ProgressReportingAgent)

    def test_returns_plain_agent_when_progress_disabled(self) -> None:
        settings = _make_settings(COMMENTS_PROGRESS_EVERY_ITERATIONS="0")
        with (
            patch("issue_agent.runner.agent_factory.CostAwareLitellmModel"),
            patch("issue_agent.runner.agent_factory.SandboxedLocalEnvironment"),
            patch("issue_agent.runner.agent_factory.load_system_prompt", return_value="p"),
        ):
            agent = build_agent(
                _make_task(),
                Path("/ws"),
                settings,
                _make_route(),
                terms=TERMS,
                on_progress=lambda i, c: None,
            )
        assert not isinstance(agent, ProgressReportingAgent)

    def test_returns_plain_agent_when_no_on_progress_callback(self) -> None:
        settings = _make_settings(COMMENTS_PROGRESS_EVERY_ITERATIONS="20")
        with (
            patch("issue_agent.runner.agent_factory.CostAwareLitellmModel"),
            patch("issue_agent.runner.agent_factory.SandboxedLocalEnvironment"),
            patch("issue_agent.runner.agent_factory.load_system_prompt", return_value="p"),
        ):
            agent = build_agent(_make_task(), Path("/ws"), settings, _make_route(), terms=TERMS)
        assert not isinstance(agent, ProgressReportingAgent)

    def test_model_receives_llm_api_key(self) -> None:
        settings = _make_settings(LLM_API_KEY="sk-test-key-123")
        task = _make_task()

        with (
            patch("issue_agent.runner.agent_factory.CostAwareLitellmModel") as mock_model_cls,
            patch("issue_agent.runner.agent_factory.SandboxedLocalEnvironment") as mock_env_cls,
            patch("issue_agent.runner.agent_factory.DefaultAgent") as mock_agent_cls,
            patch("issue_agent.runner.agent_factory.load_system_prompt") as mock_load,
        ):
            mock_model_cls.return_value = MagicMock()
            mock_env_cls.return_value = MagicMock()
            mock_agent_cls.return_value = MagicMock()
            mock_load.return_value = ""

            build_agent(task, Path("/ws"), settings, _make_route(), terms=TERMS)

        model_kwargs = mock_model_cls.call_args[1]
        assert "max_retries" in model_kwargs["model_kwargs"]


class TestRepoLlmOverrideInAgentFactory:
    """`build_agent` resolves `settings.llm_profile(route)` before constructing
    the model — a repo's `llm:` override must actually reach the LiteLLM call,
    not just the `Settings.llm_profile()` unit tests in test_config.py."""

    def _build(self, settings, route):
        with (
            patch("issue_agent.runner.agent_factory.CostAwareLitellmModel") as mock_model_cls,
            patch("issue_agent.runner.agent_factory.SandboxedLocalEnvironment"),
            patch("issue_agent.runner.agent_factory.DefaultAgent"),
            patch("issue_agent.runner.agent_factory.load_system_prompt", return_value="p"),
        ):
            build_agent(_make_task(), Path("/ws"), settings, route, terms=TERMS)
        return mock_model_cls.call_args[1]

    def test_route_llm_model_overrides_the_global_model(self):
        settings = _make_settings(LLM_MODEL="global/model")
        route = _make_route(llm=RepoLlmOverride(model="repo/model"))

        kwargs = self._build(settings, route)

        assert kwargs["model_name"] == "repo/model"

    def test_route_llm_cost_tracking_lenient_maps_to_ignore_errors(self):
        settings = _make_settings(LLM_COST_TRACKING="strict")
        route = _make_route(llm=RepoLlmOverride(cost_tracking="lenient"))

        kwargs = self._build(settings, route)

        # Proves cost_tracking/set_cache_control are read from the resolved
        # profile, not still from `settings.llm_cost_tracking` directly.
        assert kwargs["cost_tracking"] == "ignore_errors"

    def test_route_llm_reasoning_effort_overrides_the_global_value(self):
        settings = _make_settings(LLM_REASONING_EFFORT="low")
        route = _make_route(llm=RepoLlmOverride(reasoning_effort="high"))

        kwargs = self._build(settings, route)

        assert kwargs["model_kwargs"]["reasoning_effort"] == "high"

    def test_route_llm_fallback_still_uses_the_global_api_key(self):
        settings = _make_settings(
            LLM_API_KEY="sk-global",
            LLM_FALLBACK_MODEL="global/fallback",
        )
        route = _make_route(llm=RepoLlmOverride(model="repo/model"))

        kwargs = self._build(settings, route)

        # RepoLlmOverride has no api_key field — a repo overriding `model`
        # must not change which key the fallback uses.
        assert kwargs["model_kwargs"]["fallbacks"] == [
            {"model": "global/fallback", "api_key": "sk-global"}
        ]

    def test_route_without_llm_block_produces_the_current_kwargs(self):
        settings = _make_settings(LLM_MODEL="global/model", LLM_MAX_RETRIES=7)
        route = _make_route()  # no llm override at all

        kwargs = self._build(settings, route)

        assert kwargs["model_name"] == "global/model"
        assert kwargs["model_kwargs"]["max_retries"] == 7


class TestRequestTimeout:
    """A per-request timeout keeps a half-dead provider connection from
    hanging a call until task_timeout."""

    def _build(self, **overrides) -> dict:
        settings = _make_settings(**overrides)
        with (
            patch("issue_agent.runner.agent_factory.CostAwareLitellmModel") as mock_model_cls,
            patch("issue_agent.runner.agent_factory.SandboxedLocalEnvironment"),
            patch("issue_agent.runner.agent_factory.DefaultAgent"),
            patch("issue_agent.runner.agent_factory.load_system_prompt", return_value="s"),
        ):
            build_agent(_make_task(), Path("/ws/stateway"), settings, _make_route(), terms=TERMS)
        return mock_model_cls.call_args[1]["model_kwargs"]

    def test_configured_timeout_reaches_litellm(self) -> None:
        assert self._build(LLM_REQUEST_TIMEOUT_SECONDS=45)["timeout"] == 45

    def test_zero_disables_the_timeout(self) -> None:
        assert "timeout" not in self._build(LLM_REQUEST_TIMEOUT_SECONDS=0)
