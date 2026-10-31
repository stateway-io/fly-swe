"""Jinja templates the LLM sees for observations and format errors.

mini-swe-agent ships these in its YAML config files
(``minisweagent/config/default.yaml``, ``mini.yaml``,
``benchmarks/swebench.yaml``), but we build the agent in Python, so the model
falls back to ``LitellmModelConfig``'s bare defaults instead — an observation
template with **no length cap** and ``format_error_template = "{{ error }}"``.
This module ports the YAML versions, with the cap and the budget threshold
parameterized from ``Settings``.

Both are rendered with ``StrictUndefined``
(``minisweagent/models/utils/actions_toolcall.py``), so every name referenced
here must exist in the render namespace or the run dies. The observation
namespace is ``agent.get_template_vars()``: ``AgentConfig.model_dump()``
(``step_limit``, ``cost_limit``, ``wall_time_limit_seconds``) merged with the
agent's live counters (``n_model_calls``, ``model_cost``, ``elapsed_seconds``).
The format-error namespace is what ``parse_toolcall_actions`` passes:
``error``, ``actions``, ``has_tool_calls`` and ``finish_reason``.
"""

from __future__ import annotations

# Port of the tool-call variant in upstream's ``mini.yaml``/``swebench.yaml``.
# The default (``"{{ error }}"``) hands the model the raw exception with no
# hint of the expected shape; three of those in a row end the run as
# ``RepeatedFormatError``. The ``finish_reason`` branch matters here because we
# expose ``llm.max_output_tokens``: a truncated response is not a formatting
# mistake, and telling the model to "be concise" is the only useful advice.
FORMAT_ERROR_TEMPLATE = """\
{% if finish_reason is defined and (finish_reason == "length" or (finish_reason == "tool_calls" and not has_tool_calls)) -%}
Your previous response reached the output token limit (finish_reason={{ finish_reason }}) before you produced a tool call, so it was cut off. Respond more concisely and finish with exactly one bash tool call. If you need to think more, do so briefly.
{%- else -%}
Tool call error:

<error>
{{ error }}
</error>

Here is general guidance on how to submit correct toolcalls:

Every response needs to use the 'bash' tool at least once to execute commands.

Call the bash tool with your command as the argument:
- Tool: bash
- Arguments: {"command": "your_command_here"}

To finish the task, run a command whose output's first line is exactly
COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT, followed by your summary — as described
in the first message.
{%- endif %}"""


_OBSERVATION_HEADER = """\
{% if output.exception_info -%}
<exception>{{ output.exception_info }}</exception>
{% endif -%}
<returncode>{{ output.returncode }}</returncode>
"""

_OUTPUT_UNCAPPED = """\
<output>
{{ output.output -}}
</output>"""

# Verbatim from upstream's ``default.yaml``, with the literals substituted in.
_OUTPUT_CAPPED = """\
{% if output.output | length < __MAX__ -%}
<output>
{{ output.output -}}
</output>
{%- else -%}
<warning>
The output of your last command was too long.
Please try a different command that produces less output.
If you're looking at a file you can try use head, tail or sed to view a smaller number of lines selectively.
If you're using grep or find and it produced too much output, you can use a more selective search pattern.
If you really need to see something from the full command's output, you can redirect output to a file and then search in that file.
</warning>
{%- set elided_chars = output.output | length - __MAX__ -%}
<output_head>
{{ output.output[:__HEAD__] }}
</output_head>
<elided_chars>
{{ elided_chars }} characters elided
</elided_chars>
<output_tail>
{{ output.output[-__TAIL__:] }}
</output_tail>
{%- endif -%}"""

# prompts/system.md tells the agent to wind down at 80% of its allowance, but
# nothing ever told it how much it had spent. These three counters are already
# in the render namespace; this block is what makes that instruction
# executable. A limit of 0 means "no limit" in mini-swe-agent, hence the
# ``> 0`` guards.
_BUDGET_STATUS = """
{%- set step_hot = step_limit > 0 and n_model_calls >= step_limit * __FRAC__ %}
{%- set cost_hot = cost_limit > 0 and model_cost >= cost_limit * __FRAC__ %}
{%- set time_hot = wall_time_limit_seconds > 0 and elapsed_seconds >= wall_time_limit_seconds * __FRAC__ %}
{%- if step_hot or cost_hot or time_hot %}
<budget_status>
You have used {{ n_model_calls }}/{{ step_limit }} steps, ${{ "%.2f" | format(model_cost) }}/${{ "%.2f" | format(cost_limit) }} and {{ elapsed_seconds }}/{{ wall_time_limit_seconds }} seconds. You are past __PERCENT__% of at least one of these limits, and the run is stopped without warning when any one of them is reached.
Wrap up now: leave your best partial edit in the working tree and finish with the COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT signal, stating in your summary that the work is partial and what is left to do.
</budget_status>
{%- endif -%}"""


def build_observation_template(max_chars: int, warn_at_percent: int) -> str:
    """The observation template with ``max_chars`` and the budget threshold baked in.

    The cap is substituted as a literal rather than passed as a Jinja variable
    because the elision slices (``output.output[:5000]``) need constants.
    ``max_chars <= 0`` drops the elision branch entirely (uncapped, the
    upstream-default behavior); ``warn_at_percent <= 0`` drops the budget block.
    """
    if max_chars > 0:
        head = max_chars // 2
        # head + tail == max_chars exactly, so `elided_chars` is truthful for
        # an odd cap too.
        tail = max_chars - head
        body = (
            _OUTPUT_CAPPED.replace("__MAX__", str(max_chars))
            .replace("__HEAD__", str(head))
            .replace("__TAIL__", str(tail))
        )
    else:
        body = _OUTPUT_UNCAPPED

    budget = ""
    if warn_at_percent > 0:
        budget = _BUDGET_STATUS.replace("__FRAC__", f"{warn_at_percent / 100:.4f}").replace(
            "__PERCENT__", str(warn_at_percent)
        )

    return _OBSERVATION_HEADER + body + budget
