from __future__ import annotations

import time
from typing import TYPE_CHECKING

from minisweagent.models.utils.actions_toolcall import format_toolcall_observation_messages
from minisweagent.models.utils.openai_multimodal import expand_multimodal_content

if TYPE_CHECKING:
    from collections.abc import Sequence

_OBSERVATION_TEMPLATE = (
    "{% if output.exception_info %}<exception>{{output.exception_info}}</exception>\n{% endif %}"
    "<returncode>{{output.returncode}}</returncode>\n"
    "<output>\n{{output.output}}</output>"
)


class FakeModel:
    """Deterministic fake model that returns pre-configured responses.

    Duck-types the interface that ``DefaultAgent`` expects from ``model``:
    ``query()``, ``format_message()``, ``format_observation_messages()``.

    Parameters
    ----------
    responses:
        List of response dicts. Each call to ``query()`` returns the next
        response. When exhausted, the **last** response repeats (loop).
    cost_per_call:
        Cost added to ``extra.cost`` on every response (stacked on top of
        any cost already present in the response).
    query_hook:
        Optional callable ``(self, messages) -> None`` invoked at the top
        of every ``query()``. Useful for injecting side effects (sleep,
        raise, assert).
    """

    def __init__(
        self,
        responses: Sequence[dict] | None = None,
        *,
        cost_per_call: float = 0.01,
        query_hook=None,
    ) -> None:
        self.responses = list(responses) if responses is not None else [_make_bash_response("true")]
        self.cost_per_call = cost_per_call
        self.query_hook = query_hook
        self.call_count = 0

    def query(self, messages: list[dict], **kwargs) -> dict:
        if self.query_hook:
            self.query_hook(self, messages)
        idx = min(self.call_count, len(self.responses) - 1)
        resp = self.responses[idx]
        self.call_count += 1
        extra = dict(resp.get("extra", {}))
        actions = list(extra.get("actions", []))
        extra["cost"] = extra.get("cost", 0.0) + self.cost_per_call
        extra["actions"] = actions
        extra["timestamp"] = extra.get("timestamp", time.time())
        extra["response"] = extra.get("response", {"fake": True})
        return {
            "role": resp.get("role", "assistant"),
            "content": resp.get("content", ""),
            "tool_calls": resp.get("tool_calls", []),
            "extra": extra,
        }

    def format_message(self, **kwargs) -> dict:
        return expand_multimodal_content(kwargs, pattern="")

    def format_observation_messages(
        self,
        message: dict,
        outputs: list[dict],
        template_vars: dict | None = None,
    ) -> list[dict]:
        actions = message.get("extra", {}).get("actions", [])
        return format_toolcall_observation_messages(
            actions=actions,
            outputs=outputs,
            observation_template=_OBSERVATION_TEMPLATE,
            template_vars=template_vars or {},
            multimodal_regex="",
        )

    def get_template_vars(self, **kwargs) -> dict:
        return {}

    def serialize(self) -> dict:
        return {
            "info": {
                "config": {
                    "model": {"fake": True},
                    "model_type": "tests.helpers.fake_model.FakeModel",
                },
            }
        }


def _make_bash_response(command: str, *, content: str = "") -> dict:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [],
        "extra": {
            "actions": [{"command": command}],
            "cost": 0.0,
        },
    }


def edit_file_response(path: str, content: str) -> dict:
    cmd = f"mkdir -p $(dirname {path}) && cat > {path} << 'EOF'\n{content}\nEOF"
    return _make_bash_response(cmd, content=f"Editing {path}")


def exit_response() -> dict:
    return {"role": "exit", "content": "Task complete.", "extra": {"actions": [], "cost": 0.0}}
