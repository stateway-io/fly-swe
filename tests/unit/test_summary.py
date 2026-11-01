from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from issue_agent.runner.summary import summarize_block

if TYPE_CHECKING:
    from pathlib import Path


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.py").write_text("original\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    # working-tree changes the agent left behind, uncommitted
    (tmp_path / "a.py").write_text("changed by agent\n")
    (tmp_path / "b.py").write_text("new file by agent\n")
    return tmp_path


_MESSAGES = [
    {"role": "system", "content": "you are an agent"},
    {"role": "user", "content": "fix the issue"},
    {"role": "assistant", "content": "I will inspect a.py and edit it"},
    {"role": "tool", "content": "<output>done</output>"},
    {"role": "assistant", "content": "Now editing b.py to add the helper"},
]


class _FakeModel:
    def __init__(self, text: str = "handoff note", cost: float = 0.01, raises=None):
        self.text = text
        self.cost = cost
        self.raises = raises
        self.calls: list[list[dict]] = []

    def query(self, messages, **kwargs):
        self.calls.append(messages)
        if self.raises is not None:
            raise self.raises
        return {"content": self.text, "extra": {"cost": self.cost}}


class TestPolicyNever:
    def test_returns_empty(self, repo: Path) -> None:
        text, kind, cost = summarize_block(
            policy="never",
            block_error="task_timeout",
            ws_path=repo,
            messages=_MESSAGES,
            model=_FakeModel(),
            max_chars=4000,
        )
        assert (text, kind, cost) == ("", "", 0.0)


class TestMechanical:
    def test_lists_touched_files_and_recent_actions(self, repo: Path) -> None:
        text, kind, cost = summarize_block(
            policy="mechanical",
            block_error="task_timeout",
            ws_path=repo,
            messages=_MESSAGES,
            model=_FakeModel(),
            max_chars=4000,
        )
        assert kind == "mechanical"
        assert cost == 0.0
        assert "a.py" in text
        assert "b.py" in text
        assert "editing b.py" in text  # most recent assistant turn

    def test_does_not_call_the_model(self, repo: Path) -> None:
        model = _FakeModel()
        summarize_block(
            policy="mechanical",
            block_error="task_timeout",
            ws_path=repo,
            messages=_MESSAGES,
            model=model,
            max_chars=4000,
        )
        assert model.calls == []

    def test_truncated_to_max_chars(self, repo: Path) -> None:
        text, _, _ = summarize_block(
            policy="mechanical",
            block_error="task_timeout",
            ws_path=repo,
            messages=_MESSAGES,
            model=_FakeModel(),
            max_chars=40,
        )
        assert len(text) <= 40 + 40  # body + truncation marker
        assert "truncat" in text.lower()


class TestLlm:
    def test_llm_policy_calls_model_and_counts_cost(self, repo: Path) -> None:
        model = _FakeModel(text="what remains: wire the helper", cost=0.02)
        text, kind, cost = summarize_block(
            policy="llm",
            block_error="task_timeout",
            ws_path=repo,
            messages=_MESSAGES,
            model=model,
            max_chars=4000,
        )
        assert kind == "llm"
        assert cost == 0.02
        assert "wire the helper" in text
        assert len(model.calls) == 1

    def test_llm_unless_budget_degrades_to_mechanical_on_cost_block(self, repo: Path) -> None:
        model = _FakeModel()
        text, kind, cost = summarize_block(
            policy="llm_unless_budget",
            block_error="budget_exceeded",
            ws_path=repo,
            messages=_MESSAGES,
            model=model,
            max_chars=4000,
        )
        assert kind == "mechanical"
        assert cost == 0.0
        assert model.calls == []
        assert "a.py" in text

    def test_llm_unless_budget_uses_llm_on_timeout(self, repo: Path) -> None:
        model = _FakeModel()
        _, kind, _ = summarize_block(
            policy="llm_unless_budget",
            block_error="task_timeout",
            ws_path=repo,
            messages=_MESSAGES,
            model=model,
            max_chars=4000,
        )
        assert kind == "llm"
        assert len(model.calls) == 1

    def test_llm_failure_degrades_to_mechanical(self, repo: Path) -> None:
        model = _FakeModel(raises=RuntimeError("cost for model could not be determined"))
        text, kind, cost = summarize_block(
            policy="llm",
            block_error="task_timeout",
            ws_path=repo,
            messages=_MESSAGES,
            model=model,
            max_chars=4000,
        )
        assert kind == "mechanical"
        assert cost == 0.0
        assert "a.py" in text
