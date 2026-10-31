from __future__ import annotations

import pytest
from jinja2 import StrictUndefined, Template

from issue_agent.runner.templates import FORMAT_ERROR_TEMPLATE, build_observation_template

# What agent.get_template_vars() actually carries at render time: the limits
# come from AgentConfig.model_dump(), the counters from the agent itself.
_AGENT_VARS = {
    "step_limit": 50,
    "cost_limit": 2.00,
    "wall_time_limit_seconds": 900,
    "n_model_calls": 5,
    "model_cost": 0.10,
    "elapsed_seconds": 60,
}


def _observation(output: str, *, max_chars: int = 100, warn: int = 80, **var_overrides) -> str:
    return Template(
        build_observation_template(max_chars, warn), undefined=StrictUndefined
    ).render(
        output={"output": output, "returncode": 0, "exception_info": ""},
        **{**_AGENT_VARS, **var_overrides},
    )


class TestOutputTruncation:
    def test_short_output_passes_through_whole(self) -> None:
        rendered = _observation("hello world")
        assert "<output>" in rendered
        assert "hello world" in rendered
        assert "<output_head>" not in rendered

    def test_long_output_is_elided_head_and_tail(self) -> None:
        rendered = _observation("A" * 60 + "B" * 60 + "C" * 60)

        assert "<output_head>" in rendered
        assert "<output_tail>" in rendered
        assert "The output of your last command was too long" in rendered
        # 180 chars in, 100 kept (50 head + 50 tail) -> 80 elided.
        assert "80 characters elided" in rendered
        # The middle is gone, the ends survive.
        assert "B" * 60 not in rendered
        assert rendered.count("A") >= 50
        assert rendered.count("C") >= 50

    def test_elided_count_is_exact_for_an_odd_cap(self) -> None:
        rendered = _observation("X" * 200, max_chars=101)
        assert "99 characters elided" in rendered

    def test_zero_disables_the_cap(self) -> None:
        rendered = _observation("Z" * 5000, max_chars=0)
        assert "<output_head>" not in rendered
        assert "Z" * 5000 in rendered

    def test_returncode_and_exception_are_preserved(self) -> None:
        rendered = Template(
            build_observation_template(100, 80), undefined=StrictUndefined
        ).render(
            output={"output": "x", "returncode": 3, "exception_info": "timed out"},
            **_AGENT_VARS,
        )
        assert "<returncode>3</returncode>" in rendered
        assert "<exception>timed out</exception>" in rendered


class TestBudgetStatus:
    """prompts/system.md tells the agent to wind down at 80% of its budget;
    this block is the only thing that tells it where it stands."""

    def test_absent_below_the_threshold(self) -> None:
        assert "<budget_status>" not in _observation("ok")

    @pytest.mark.parametrize(
        "hot",
        [
            {"n_model_calls": 40},  # 40/50 steps
            {"model_cost": 1.60},  # $1.60/$2.00
            {"elapsed_seconds": 720},  # 720/900 s
        ],
    )
    def test_present_at_the_threshold_of_each_limit(self, hot: dict) -> None:
        rendered = _observation("ok", **hot)
        assert "<budget_status>" in rendered
        assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in rendered

    def test_reports_the_actual_numbers(self) -> None:
        rendered = _observation("ok", n_model_calls=45, model_cost=1.5, elapsed_seconds=300)
        assert "45/50 steps" in rendered
        assert "$1.50/$2.00" in rendered
        assert "300/900 seconds" in rendered

    def test_zero_percent_disables_the_block(self) -> None:
        assert "<budget_status>" not in _observation("ok", warn=0, n_model_calls=49)

    def test_a_limit_of_zero_means_no_limit(self) -> None:
        """mini-swe-agent treats 0 as unlimited — n_calls >= 0 * frac must not
        fire the warning for every single observation."""
        rendered = _observation(
            "ok",
            step_limit=0,
            cost_limit=0.0,
            wall_time_limit_seconds=0,
            n_model_calls=999,
        )
        assert "<budget_status>" not in rendered


class TestStrictUndefined:
    """Both templates are rendered with StrictUndefined by mini-swe-agent; a
    name that isn't in the namespace kills the run, not just the message."""

    def test_observation_renders_with_the_real_agent_namespace(self) -> None:
        from minisweagent.agents.default import AgentConfig, DefaultAgent

        agent = DefaultAgent(
            model=_NullModel(),
            env=_NullEnv(),
            config_class=AgentConfig,
            system_template="",
            instance_template="",
            step_limit=10,
            cost_limit=1.0,
            wall_time_limit_seconds=60,
        )
        Template(build_observation_template(100, 80), undefined=StrictUndefined).render(
            output={"output": "x", "returncode": 0, "exception_info": ""},
            **agent.get_template_vars(),
        )

    def test_format_error_renders_on_a_truncated_response(self) -> None:
        rendered = Template(FORMAT_ERROR_TEMPLATE, undefined=StrictUndefined).render(
            error="", actions=[], has_tool_calls=False, finish_reason="length"
        )
        assert "output token limit" in rendered

    def test_format_error_teaches_the_expected_shape(self) -> None:
        rendered = Template(FORMAT_ERROR_TEMPLATE, undefined=StrictUndefined).render(
            error="Unknown tool 'edit'.", actions=[], has_tool_calls=True, finish_reason="stop"
        )
        assert "Unknown tool 'edit'." in rendered
        assert "bash" in rendered
        assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in rendered


class _NullModel:
    def get_template_vars(self) -> dict:
        return {}


class _NullEnv:
    def get_template_vars(self) -> dict:
        return {}
