# Study: mini-SWE-agent v2 API

> **Non-normative research snapshot.** A reference dig through the
> mini-swe-agent API as integrated into this project; not kept in sync with
> the codebase. For current, authoritative behaviour see
> [`docs/operations.md`](../operations.md) and `src/issue_agent/runner/`.

**Version:** 2.4.6 (PyPI)
**Date:** 2026-05-21 (section 12 added 2026-09-12, on the 2.4.6 bump)
**Status:** Historical snapshot

---

## 1. Package and Import Paths

```python
from minisweagent.agents.default import DefaultAgent, AgentConfig
from minisweagent.models.litellm_model import LitellmModel, LitellmModelConfig
from minisweagent.environments.local import LocalEnvironment, LocalEnvironmentConfig
```

---

## 2. LitellmModelConfig

Constructor (`**kwargs` → Pydantic `BaseModel`):

| Parameter | Type | Default | Description |
|---|---|---|---|
| `model_name` | `str` | (required) | `"anthropic/claude-sonnet-4-5-20250929"` — recommended to include the provider |
| `model_kwargs` | `dict[str, Any]` | `{}` | Extra arguments for `litellm.completion()`. E.g.: `{"max_retries": 5}` |
| `set_cache_control` | `"default_end" \| None` | `None` | Automatic prompt caching for Anthropic |
| `cost_tracking` | `"default" \| "ignore_errors"` | `"default"` | Cost tracking mode |
| `litellm_model_registry` | `Path \| str \| None` | env var | Per-model cost registry |
| `format_error_template` | `str` | `"{{ error }}"` | Template for format errors — **see section 12** |
| `observation_template` | `str` | no length cap | Jinja2 template for observations — **see section 12** |
| `multimodal_regex` | `str` | `""` | Regex for multimodal content |

---

## 3. LitellmModel

```python
model = LitellmModel(
    config=LitellmModelConfig(
        model_name="anthropic/claude-sonnet-4-5-20250929",
        cost_tracking="default",
        set_cache_control="default_end",
        model_kwargs={"max_retries": 5},
    )
)
```

### Attributes

- `self.config` → `LitellmModelConfig`
- **Does not have `self.cost` or `self.n_calls`** — cost is reported per call in the return value of `query()`.

### Methods

```python
def query(self, messages: list[dict[str, str]], **kwargs) -> dict:
    """Query the LLM via LiteLLM.

    Returns dict with structure:
    {
        "role": "assistant",
        "content": "...",
        "tool_calls": [...],
        "extra": {
            "actions": [{"command": "...", "tool_call_id": "..."}, ...],
            "response": <raw>,
            "cost": <float>,         # ← cost of this call
            "timestamp": <float>,
        }
    }
    """
```

### Internal Cost Tracking

`LitellmModel` delegates cost tracking to the global singleton `GLOBAL_MODEL_STATS`:

```python
class GlobalModelStats:
    @property
    def cost(self) -> float
    @property
    def n_calls(self) -> float
```

--- 

## 4. LocalEnvironmentConfig

```python
class LocalEnvironmentConfig(BaseModel):
    cwd: str = ""
    env: dict[str, str] = {}
    timeout: int = 30
```

---

## 5. LocalEnvironment

```python
env = LocalEnvironment(
    config=LocalEnvironmentConfig(
        cwd=str(workspace_path),
        timeout=settings.command_timeout_seconds,
    )
)
```

### Attributes

- `self.config` → `LocalEnvironmentConfig`

### Methods

```python
def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
    """Executes a bash command via subprocess.run.

    Args:
        action: dict with key "command" (string)
        cwd: working directory (overrides config.cwd)
        timeout: timeout in seconds (overrides config.timeout)

    Returns:
        {"output": str (stdout+stderr), "returncode": int, "exception_info": str}
    """
```

Important: `shell=True` in `subprocess.run` — commands are executed via shell, not direct exec.

---

## 6. AgentConfig

```python
class AgentConfig(BaseModel):
    system_template: str      # System prompt (first message)
    instance_template: str    # User message (second message)
    step_limit: int = 0       # Maximum number of steps (0 = unlimited)
    cost_limit: float = 3.0   # Stops the agent when cost EXCEEDS this value
    output_path: Path | None = None  # Save trajectory to this path
```

---

## 7. DefaultAgent

```python
agent = DefaultAgent(
    model=model,
    env=env,
    config=AgentConfig(
        system_template=load_system_prompt(task.label_category),
        instance_template=build_user_message(task),
        step_limit=settings.max_iterations,
        cost_limit=settings.max_budget_per_task_usd,
    ),
)
```

### Main Attributes

| Attribute | Type | Initial | Description |
|---|---|---|---|
| `model` | `Model` | (constructor) | Model instance |
| `env` | `Environment` | (constructor) | Execution environment |
| `config` | `AgentConfig` | (constructor) | Configuration |
| `messages` | `list[dict]` | `[]` | Full conversation history |
| **`cost`** | **`float`** | **`0.0`** | **Accumulated cost (plain attr, not @property)** |
| **`n_calls`** | **`int`** | **`0`** | **Number of queries to the model (plain attr, not @property)** |

### ⚠️ Common mistake: `agent.model.cost`

Early design notes used `agent.model.cost` — **this is incorrect**. The cost is accumulated on
the agent itself as `agent.cost`, not on the model. The correct property is:

```python
agent.run(user_message)
cost = agent.cost          # ← correct
n_calls = agent.n_calls    # ← correct
```

### Methods

```python
def run(self, task: str = "", **kwargs) -> dict:
    """Runs agent.step() in a loop until it finishes.

    Returns:
        dict with keys "exit_status" and "submission" (the last extra from the exit message).
    """

def step(self) -> list[dict]:
    """Query model + execute actions. One complete cycle."""
```

### Internal Flow of `run()`:

```
1. Renders templates (system + instance)
2. Adds messages to the history
3. Loop:
   a. step() → query() → execute_actions()
   b. If InterruptAgentFlow: adds messages, continues
   c. If unhandled Exception: handle_uncaught_exception()
   d. If the last message has role="exit": break
4. Returns the last extra (exit_status, submission)
```

### Native Limits

In `DefaultAgent.query()`:

```python
def query(self) -> dict:
    if 0 < self.config.step_limit <= self.n_calls or 0 < self.config.cost_limit <= self.cost:
        raise LimitsExceeded(...)
    self.n_calls += 1
    message = self.model.query(self.messages)
    self.cost += message.get("extra", {}).get("cost", 0.0)
    ...
```

- `step_limit` and `cost_limit` are checked **before** each query
- When exceeded, `LimitsExceeded` (a subclass of `InterruptAgentFlow`) is raised
- `cost` and `n_calls` are incremented **after** the guard — if the limit was already reached, the value does not increment

### Jinja2 Templates

The `system_template` and `instance_template` fields are Jinja2 strings.
If there are no variables to interpolate, literal strings work fine.

Available template vars:
- `n_model_calls`, `model_cost` (auto-injected by the agent)
- `task` (passed as an arg to `run()`)
- `AgentConfig` attributes

⚠️ If the string contains literal `{` or `}` (e.g.: `${cost}`), escape them as `{{` / `}}`
to avoid a Jinja2 template error.

---

## 8. Exceptions

| Exception | Inherits from | When it occurs |
|---|---|---|
| `InterruptAgentFlow` | `Exception` | Base for interruption flows |
| `LimitsExceeded` | `InterruptAgentFlow` | step_limit or cost_limit exceeded |
| `Submitted` | `InterruptAgentFlow` | Agent completed the task (via `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`) |
| `FormatError` | `InterruptAgentFlow` | Model output could not be parsed |
| `UserInterruption` | `InterruptAgentFlow` | User interrupted |

---

## 9. Design concept → API mapping

| Design concept | mini-SWE-agent API |
|---|---|
| Agent engine | `DefaultAgent` |
| Agent config | `AgentConfig(system_template, instance_template, step_limit, cost_limit)` |
| LLM model | `LitellmModel(config=LitellmModelConfig(...))` |
| Environment | `LocalEnvironment(config=LocalEnvironmentConfig(cwd, timeout))` |
| Run the agent | `agent.run(task_string)` |
| Post-run cost | `agent.cost` (float) |
| Post-run iterations | `agent.n_calls` (int) |
| Budget per-task | `AgentConfig.cost_limit` (native) |
| Max steps | `AgentConfig.step_limit` (native) |
| System prompt | `AgentConfig.system_template` (string, Jinja2) |
| Task prompt | `AgentConfig.instance_template` (string, Jinja2) |
| Workspace path | `LocalEnvironmentConfig.cwd` |
| Timeout per command | `LocalEnvironmentConfig.timeout` |

---

## 10. Complete Example (Project Skeleton)

```python
from pathlib import Path

from minisweagent.agents.default import DefaultAgent, AgentConfig
from minisweagent.models.litellm_model import LitellmModel, LitellmModelConfig
from minisweagent.environments.local import LocalEnvironment

from issue_agent.config import Settings
from issue_agent.queue.models import Task
from issue_agent.runner.prompts import load_system_prompt, build_user_message


def build_agent(task: Task, workspace_path: Path, settings: Settings) -> DefaultAgent:
    model = LitellmModel(
        config=LitellmModelConfig(
            model_name=settings.llm_model,
            cost_tracking="default",
            set_cache_control="default_end",
            model_kwargs={"max_retries": settings.llm_max_retries},
        )
    )

    env = LocalEnvironment(
        config=LocalEnvironmentConfig(
            cwd=str(workspace_path),
            timeout=settings.command_timeout_seconds,
        )
    )

    agent = DefaultAgent(
        model=model,
        env=env,
        config=AgentConfig(
            system_template=load_system_prompt(task.label_category),
            instance_template=build_user_message(task),
            step_limit=settings.max_iterations,
            cost_limit=settings.max_budget_per_task_usd,
        ),
    )

    return agent


# Usage:
# agent = build_agent(task, ws_path, settings)
# result = agent.run(task_message)
# cost = agent.cost
# calls = agent.n_calls
```

---

## 11. Implementation Notes

1. **AgentConfig accepts keyword args** — `config_class=AgentConfig` is the default, and the kwargs
   (`system_template`, `step_limit`, etc.) are passed as `**kwargs`.
2. **`LitellmModelConfig` also accepts kwargs** — the constructor uses `config_class=LitellmModelConfig, **kwargs`.
3. **`LocalEnvironment` likewise** — `config_class=LocalEnvironmentConfig, **kwargs`.
4. **Cost is `agent.cost`, not `agent.model.cost`**.
5. **step_limit=0 = unlimited**, `cost_limit` default is 3.0.
6. **`agent.run()` returns a dict**, not the agent itself. The agent is mutated in-place.
7. **LimitsExceeded is an InterruptAgentFlow**, handled internally by `agent.run()`.
8. **Thread safety**: only relevant if sharing a `DefaultAgent` across threads (not the case for the MVP — single-thread worker).

---

## 12. YAML defaults that the Python constructor does NOT give you

The single most important gotcha of driving mini-swe-agent as a library: the
package's behavior "out of the box" as documented (and as measured on
SWE-bench) comes from its **YAML config files**, not from the Pydantic model
defaults. Building the agent in Python — as `runner/agent_factory.py` does —
silently opts out of all of them.

| Setting | `minisweagent/config/*.yaml` | Pydantic default | We now |
|---|---|---|---|
| `environment.env` | `PAGER`/`MANPAGER`/`LESS`/`PIP_PROGRESS_BAR`/`TQDM_DISABLE` | `{}` | `_NONINTERACTIVE_ENV` in `runner/environment.py` (+ `GIT_PAGER`, `GIT_TERMINAL_PROMPT`, `DEBIAN_FRONTEND`, `PYTHONUNBUFFERED`) |
| `model.observation_template` | elides above 10 000 chars into head/tail | `<output>{{output.output}}</output>`, **uncapped** | `build_observation_template()` in `runner/templates.py`, cap from `llm.observation_max_chars` |
| `model.format_error_template` | explains the expected tool call, plus a `finish_reason == "length"` branch | `"{{ error }}"` | `FORMAT_ERROR_TEMPLATE` in `runner/templates.py` |
| `model.model_kwargs.drop_params` | `true` in every config | unset | **not adopted** — a provider rejecting a parameter still fails the run |

Why each one matters here:

- **Pager/progress-bar variables.** A command that opens a pager hangs until
  `command_timeout_seconds` kills it; `\r`-driven progress bars are billed as
  tokens on every subsequent step.
- **Observation cap.** The message history is re-sent on every step, so one
  uncapped `pytest -v` is paid for repeatedly and can reach
  `ContextWindowExceededError` — which is in `LitellmModel.abort_exceptions`
  and therefore kills the run outright.
- **Format-error template.** `DefaultAgent` ends the run after
  `max_consecutive_format_errors` (3) unreadable responses; the default
  message tells the model nothing about the shape it should have used. Our
  `MiniSWERunner` maps that exit status to `format_error`.

### Beyond upstream: `<budget_status>`

`prompts/system.md` instructs the agent to wind down at 80% of its budget, but
nothing in mini-swe-agent ever tells the agent its usage. Since
`DefaultAgent.get_template_vars()` already exposes `n_model_calls`,
`model_cost` and `elapsed_seconds` — and `AgentConfig.model_dump()` the three
limits — our observation template appends a `<budget_status>` block past
`budgets.warn_at_percent`. Note that a limit of `0` means *unlimited*, so
every comparison is guarded with `> 0`.

Both templates are rendered with `StrictUndefined`
(`minisweagent/models/utils/actions_toolcall.py`): a name missing from the
namespace raises at render time and takes the run down with it.
