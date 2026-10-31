from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from issue_agent.logging import get_logger
from issue_agent.policy.label_router import (
    ALL_CATEGORIES,
    DEFAULT_DIAGNOSE_CATEGORIES,
    DEFAULT_TRIAGE_ONLY_CATEGORIES,
)
from issue_agent.providers.settings import SETTINGS_BY_TYPE, ProviderSettings

logger = get_logger(__name__)

_PACKAGE_DIR = Path(__file__).resolve().parent  # src/issue_agent
_AGENT_ROOT_DIR = _PACKAGE_DIR.parents[1]  # raiz do repo em dev, /app no container

_DEFAULT_CONFIG_FILE = _AGENT_ROOT_DIR / "config.yaml"
_DEFAULT_ENV_FILE = _AGENT_ROOT_DIR / ".env"

DEFAULT_CATEGORY_PROMPT_MAP: dict[str, str] = {
    "bug": "bug_fix.md",
    "docs": "doc_fix.md",
    "chore": "bug_fix.md",
    "agent_fix_please": "bug_fix.md",
}
# NOTE: "question", "feature_request", and "high_severity_bug" are
# intentionally absent — by default they route to LabelAction.TRIAGE_ONLY in
# policy/label_router.py, which never calls load_system_prompt() (it uses
# triage/builder.py's build_triage_comment() instead). Mapping them here
# would point at a prompt file that no longer exists (prompts/triage_only.md
# was removed as dead code). Which categories actually route to TRIAGE_ONLY
# vs. DIAGNOSE vs. FIX is configurable — see policy_triage_only_categories /
# policy_diagnose_categories below — so this NOTE describes the *default*,
# not a hardcoded fact.


class ProjectProfile(BaseModel):
    """Per-target-repo project profile (test/lint commands, file heuristics).

    Every field is optional — an empty string/list means the agent discovers
    the value on its own via the prompt (R5.6.1). Nested under RepoRoute.project
    in config.yaml's `repos:` list; used to be Settings-level globals shared by
    every repo, now scoped to the repo pair that configures it.
    """

    test_command: str = ""
    lint_command: str = ""
    relevant_extensions: list[str] = []
    protected_paths: list[str] = []
    # Toolchain selection. "auto" lets the harness detect the stack from
    # project files (e.g. package.json -> nodejs). A concrete name forces
    # that plugin, and "none" disables automatic toolchain provisioning.
    toolchain: str = "auto"
    # Optional explicit Node.js version for this repo. Empty means discover
    # from .nvmrc / package.json engines / default LTS.
    node_version: str = ""


class RepoLlmFallbackOverride(BaseModel):
    """Per-repo override of the global `llm.fallback` block. See RepoLlmOverride."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    base_url: str | None = None


class RepoLlmOverride(BaseModel):
    """Per-repo override of the global `llm:` block (Settings.llm_profile).

    Every field defaults to `None`, meaning "inherit the global value" — the
    merge in `Settings.llm_profile()` is based on *presence* in
    `model_fields_set`, not on the resolved value being `None`, so an
    explicit `null` in YAML can still reset a global value back to "let the
    provider decide" for the fields where that is meaningful
    (`temperature`, `max_output_tokens`, `reasoning_effort`) or disable
    failover entirely (`fallback: null`).

    `model`, `provider`, `base_url`, `max_retries`, `cost_tracking`,
    `prompt_caching`, and `observation_max_chars` are deliberately typed
    *without* a `| None` union even though their default is `None`: pydantic
    does not validate field defaults, so an absent key still resolves to
    `None` (and is excluded from `model_fields_set`), but an explicit
    `null` in YAML fails validation instead of silently resolving to `None`.
    That distinction matters here — `None` is not a safe stand-in for "no
    override" on these fields the way it is for `temperature`: it would
    silently disarm the cost-tracking guard (`llm_cost_tracking`), turn off
    prompt caching, or crash agent construction (`observation_max_chars`
    feeds a `> 0` comparison). Writing the field at all is a mistake, and it
    should fail loudly at `Settings()` construction, not at runtime.

    `api_key` / `fallback.api_key` are intentionally absent — API keys stay
    global (never read from config.yaml) and `extra="forbid"` turns an
    attempt to set one here into an immediate validation error rather than a
    silently-ignored key.
    """

    model_config = ConfigDict(extra="forbid")

    model: str = None  # type: ignore[assignment]
    provider: str = None  # type: ignore[assignment]
    base_url: str = None  # type: ignore[assignment]
    max_retries: int = None  # type: ignore[assignment]
    cost_tracking: Literal["strict", "lenient"] = None  # type: ignore[assignment]
    prompt_caching: Literal["default_end", "none"] = None  # type: ignore[assignment]
    observation_max_chars: int = None  # type: ignore[assignment]
    temperature: float | None = None
    max_output_tokens: int | None = None
    # LiteLLM's own vocabulary (llms/anthropic/chat/transformation.py) — not
    # just the five Anthropic effort levels: "minimal" is a legacy-model-only
    # alias for "low", and "none" is an explicit "strip thinking" distinct
    # from leaving the field unset (which never sends the parameter at all).
    reasoning_effort: (
        Literal["minimal", "low", "medium", "high", "xhigh", "max", "none"] | None
    ) = None
    fallback: RepoLlmFallbackOverride | None = None


class RepoRoute(BaseModel):
    """One (issue-repo, code-repo) pair and how the agent should work it.

    `issues` is the repo the webhook listens on; `target` is where the PR
    lands (defaults to `issues` when omitted, via `target_repo`). `workflow`
    picks the branching strategy (R5.3.1): trunk_based branches
    `<branch_prefix>/issue-<n>-<slug>` off `base_branch`; gitflow branches
    `<base_branch>-<slug>` off `base_branch` itself (typically `dev`).
    """

    issues: str
    target: str | None = None
    # The provider instance (`providers.<id>`) the issues repo lives on.
    # Optional with a single instance; Settings fills it in.
    provider: str | None = None
    base_branch: str = "main"
    workflow: Literal["trunk_based", "gitflow"] = "trunk_based"
    branch_prefix: str = "agent"
    draft: bool = False
    labels: list[str] = []
    project: ProjectProfile = ProjectProfile()
    # Per-repo override of the global `llm:` block — see RepoLlmOverride and
    # Settings.llm_profile(). None = this repo uses the global LLM config
    # unchanged.
    llm: RepoLlmOverride | None = None

    @property
    def target_repo(self) -> str:
        return self.target or self.issues


class LlmProfile(BaseModel):
    """The 14 LLM values fully resolved for one task — the global `llm:`
    block with any per-repo override applied. Built by `Settings.llm_profile()`,
    never constructed directly. Frozen: a profile is a snapshot for one task,
    not something a caller should mutate after the fact.
    """

    model_config = ConfigDict(frozen=True)

    model: str
    provider: str
    base_url: str
    max_retries: int
    cost_tracking: Literal["strict", "lenient"]
    prompt_caching: Literal["default_end", "none"]
    temperature: float | None
    max_output_tokens: int | None
    reasoning_effort: (
        Literal["minimal", "low", "medium", "high", "xhigh", "max", "none"] | None
    )
    observation_max_chars: int
    fallback_model: str
    fallback_base_url: str
    # Always sourced from the global Settings fields — RepoLlmOverride has no
    # api_key field, so there is no path where a caller forgets the secrets.
    api_key: str
    fallback_api_key: str


def resolve_secret_path(path_str: str) -> Path:
    p = Path(path_str).expanduser()
    if p.is_absolute():
        return p
    if p.exists():
        return p.resolve()
    alt = (_AGENT_ROOT_DIR / path_str).resolve()
    if alt.exists():
        return alt
    return p.resolve()


# Removed in 1.0.0 (PRD §3.5): a hard break fails loudly instead of being
# silently ignored.
_LEGACY_SECTIONS = ("github", "github_app")

# A provider instance id names the webhook path and the env prefix
# (`PROVIDERS__<ID>__...`); `__` is the env nesting delimiter.
_INSTANCE_ID_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")


def _reject_yaml_secrets(instance_id: str, instance: Any) -> None:
    """Secrets come from env only (PROVIDERS__<ID>__<FIELD>), never from
    config.yaml — fail instead of loading one from a committed file."""
    if not isinstance(instance, dict):
        return
    model = SETTINGS_BY_TYPE.get(instance.get("type"))
    if model is None:
        return
    found = sorted(model.secret_fields & instance.keys())
    if found:
        env = ", ".join(f"PROVIDERS__{instance_id.upper()}__{f.upper()}" for f in found)
        raise ValueError(
            f"providers.{instance_id}: {', '.join(found)} must not be in "
            f"config.yaml — set {env} in the environment"
        )


def flatten_yaml_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten the nested config.yaml structure into Settings' flat validation aliases.

    `repos` is the one section that isn't flattened into scalar aliases: it
    stays a list of raw dicts under `REPO_ROUTES`, and Settings.repo_routes
    (list[RepoRoute]) validates each item when the Settings model is built —
    this function deliberately does not pre-validate, so a malformed entry
    surfaces as a normal pydantic validation error at Settings() construction
    time. Every pair is resolved by its issue repo at dispatch time (R5.2/R5.3);
    `target` omitted on an item means its code repo is its own issue repo.
    """
    flat: dict[str, Any] = {}

    legacy = [key for key in _LEGACY_SECTIONS if key in raw]
    if legacy:
        raise ValueError(
            f"config.yaml sections {', '.join(legacy)} were replaced by "
            "`providers.<id>` in 1.0.0 — see the migration guide in "
            "docs/operations.md"
        )

    agent = raw.get("agent") or {}
    if "name" in agent:
        flat["AGENT_NAME"] = agent["name"]

    providers = raw.get("providers") or {}
    if not isinstance(providers, dict):
        raise ValueError("config.yaml `providers` must be a mapping of instance ids")
    for instance_id, instance in providers.items():
        _reject_yaml_secrets(instance_id, instance)
    if providers:
        flat["PROVIDERS"] = providers

    repos = raw.get("repos") or []
    if repos:
        flat["REPO_ROUTES"] = repos

    llm = raw.get("llm") or {}
    if "model" in llm:
        flat["LLM_MODEL"] = llm["model"]
    if "base_url" in llm:
        flat["LLM_BASE_URL"] = llm["base_url"]
    if "max_retries" in llm:
        flat["LLM_MAX_RETRIES"] = llm["max_retries"]
    if "request_timeout_seconds" in llm:
        flat["LLM_REQUEST_TIMEOUT_SECONDS"] = llm["request_timeout_seconds"]
    if "provider" in llm:
        flat["LLM_PROVIDER"] = llm["provider"]
    if "cost_tracking" in llm:
        flat["LLM_COST_TRACKING"] = llm["cost_tracking"]
    if "prompt_caching" in llm:
        flat["LLM_PROMPT_CACHING"] = llm["prompt_caching"]
    if "temperature" in llm:
        flat["LLM_TEMPERATURE"] = llm["temperature"]
    if "max_output_tokens" in llm:
        flat["LLM_MAX_OUTPUT_TOKENS"] = llm["max_output_tokens"]
    if "reasoning_effort" in llm:
        flat["LLM_REASONING_EFFORT"] = llm["reasoning_effort"]
    if "observation_max_chars" in llm:
        flat["LLM_OBSERVATION_MAX_CHARS"] = llm["observation_max_chars"]
    fallback = llm.get("fallback") or {}
    if "model" in fallback:
        flat["LLM_FALLBACK_MODEL"] = fallback["model"]
    if "base_url" in fallback:
        flat["LLM_FALLBACK_BASE_URL"] = fallback["base_url"]
    # fallback.api_key is deliberately NOT read here — it's a secret and only
    # comes from the LLM_FALLBACK_API_KEY env var (R5.1.3).

    budgets = raw.get("budgets") or {}
    if "max_iterations" in budgets:
        flat["MAX_ITERATIONS"] = budgets["max_iterations"]
    if "max_cost_per_task_usd" in budgets:
        flat["MAX_BUDGET_PER_TASK_USD"] = budgets["max_cost_per_task_usd"]
    if "max_daily_cost_usd" in budgets:
        flat["MAX_DAILY_BUDGET_USD"] = budgets["max_daily_cost_usd"]
    if "task_timeout_seconds" in budgets:
        flat["TASK_TIMEOUT_SECONDS"] = budgets["task_timeout_seconds"]
    if "command_timeout_seconds" in budgets:
        flat["COMMAND_TIMEOUT_SECONDS"] = budgets["command_timeout_seconds"]
    if "max_task_retries" in budgets:
        flat["MAX_TASK_RETRIES"] = budgets["max_task_retries"]
    if "warn_at_percent" in budgets:
        flat["BUDGET_WARN_AT_PERCENT"] = budgets["warn_at_percent"]

    worker = raw.get("worker") or {}
    if "poll_interval_seconds" in worker:
        flat["POLL_INTERVAL_SECONDS"] = worker["poll_interval_seconds"]
    if "orphan_timeout_seconds" in worker:
        flat["ORPHAN_TIMEOUT_SECONDS"] = worker["orphan_timeout_seconds"]
    if "backoff_schedule_seconds" in worker:
        flat["BACKOFF_SCHEDULE_SECONDS"] = worker["backoff_schedule_seconds"]

    pr = raw.get("pr") or {}
    if "slug_max_length" in pr:
        flat["PR_SLUG_MAX_LENGTH"] = pr["slug_max_length"]
    if "label" in pr:
        flat["PR_LABEL_AGENT_GENERATED"] = pr["label"]

    git = raw.get("git") or {}
    if "clone_depth" in git:
        flat["GIT_CLONE_DEPTH"] = git["clone_depth"]

    git_identity = raw.get("git_identity") or {}
    if "user_name" in git_identity:
        flat["GIT_USER_NAME"] = git_identity["user_name"]
    if "user_email" in git_identity:
        flat["GIT_USER_EMAIL"] = git_identity["user_email"]

    metrics = raw.get("metrics") or {}
    if "port" in metrics:
        flat["METRICS_PORT"] = metrics["port"]
    if "task_duration_buckets_seconds" in metrics:
        flat["METRICS_TASK_DURATION_BUCKETS"] = metrics["task_duration_buckets_seconds"]
    if "iteration_buckets" in metrics:
        flat["METRICS_ITERATION_BUCKETS"] = metrics["iteration_buckets"]

    prompts = raw.get("prompts") or {}
    if "category_map" in prompts:
        flat["PROMPT_CATEGORY_MAP"] = prompts["category_map"]

    policy = raw.get("policy") or {}
    if "triage_only_categories" in policy:
        flat["POLICY_TRIAGE_ONLY_CATEGORIES"] = policy["triage_only_categories"]
    if "diagnose_categories" in policy:
        flat["POLICY_DIAGNOSE_CATEGORIES"] = policy["diagnose_categories"]

    comments = raw.get("comments") or {}
    if "initial_comment" in comments:
        flat["COMMENTS_INITIAL_COMMENT"] = comments["initial_comment"]
    if "progress_every_iterations" in comments:
        flat["COMMENTS_PROGRESS_EVERY_ITERATIONS"] = comments["progress_every_iterations"]
    if "progress_max_per_run" in comments:
        flat["COMMENTS_PROGRESS_MAX_PER_RUN"] = comments["progress_max_per_run"]

    author_filter = comments.get("author_filter") or {}
    if "enabled" in author_filter:
        flat["COMMENTS_AUTHOR_FILTER_ENABLED"] = author_filter["enabled"]
    if "allow_issue_author" in author_filter:
        flat["COMMENTS_AUTHOR_FILTER_ALLOW_ISSUE_AUTHOR"] = author_filter[
            "allow_issue_author"
        ]
    if "allow_associations" in author_filter:
        flat["COMMENTS_AUTHOR_FILTER_ALLOW_ASSOCIATIONS"] = author_filter[
            "allow_associations"
        ]
    if "allowlist" in author_filter:
        flat["COMMENTS_AUTHOR_FILTER_ALLOWLIST"] = author_filter["allowlist"]

    comments_context = comments.get("context") or {}
    if "max_comments" in comments_context:
        flat["COMMENTS_CONTEXT_MAX_COMMENTS"] = comments_context["max_comments"]
    if "max_chars_per_comment" in comments_context:
        flat["COMMENTS_CONTEXT_MAX_CHARS_PER_COMMENT"] = comments_context[
            "max_chars_per_comment"
        ]

    resume = raw.get("resume") or {}
    if "enabled" in resume:
        flat["RESUME_ENABLED"] = resume["enabled"]
    if "push_wip_on_block" in resume:
        flat["RESUME_PUSH_WIP_ON_BLOCK"] = resume["push_wip_on_block"]
    if "max_attempts" in resume:
        flat["RESUME_MAX_ATTEMPTS"] = resume["max_attempts"]
    if "summary_on_block" in resume:
        flat["RESUME_SUMMARY_ON_BLOCK"] = resume["summary_on_block"]
    if "summary_max_chars" in resume:
        flat["RESUME_SUMMARY_MAX_CHARS"] = resume["summary_max_chars"]
    if "retry_on_test_failure" in resume:
        flat["RESUME_RETRY_ON_TEST_FAILURE"] = resume["retry_on_test_failure"]
    if "reconcile_interval_minutes" in resume:
        flat["RESUME_RECONCILE_INTERVAL_MINUTES"] = resume["reconcile_interval_minutes"]
    if "reconcile_max_per_cycle" in resume:
        flat["RESUME_RECONCILE_MAX_PER_CYCLE"] = resume["reconcile_max_per_cycle"]

    retention = raw.get("retention") or {}
    if "events_days" in retention:
        flat["EVENTS_RETENTION_DAYS"] = retention["events_days"]
    if "db_days" in retention:
        flat["DB_RETENTION_DAYS"] = retention["db_days"]
    if "audit_days" in retention:
        flat["AUDIT_RETENTION_DAYS"] = retention["audit_days"]
    if "cleanup_interval_minutes" in retention:
        flat["CLEANUP_INTERVAL_MINUTES"] = retention["cleanup_interval_minutes"]

    toolchains = raw.get("toolchains") or {}
    if "enabled" in toolchains:
        flat["TOOLCHAINS_ENABLED"] = toolchains["enabled"]
    if "tools_root" in toolchains:
        flat["TOOLCHAINS_TOOLS_ROOT"] = toolchains["tools_root"]
    if "install_timeout_seconds" in toolchains:
        flat["TOOLCHAINS_INSTALL_TIMEOUT_SECONDS"] = toolchains["install_timeout_seconds"]
    if "lock_timeout_seconds" in toolchains:
        flat["TOOLCHAINS_LOCK_TIMEOUT_SECONDS"] = toolchains["lock_timeout_seconds"]
    nodejs = toolchains.get("nodejs") or {}
    if "default_version" in nodejs:
        flat["TOOLCHAINS_NODEJS_DEFAULT_VERSION"] = nodejs["default_version"]
    if "mirror" in nodejs:
        flat["TOOLCHAINS_NODEJS_MIRROR"] = nodejs["mirror"]
    if "max_old_space_percent" in nodejs:
        flat["TOOLCHAINS_NODEJS_MAX_OLD_SPACE_PERCENT"] = nodejs["max_old_space_percent"]
    if "max_old_space_mb" in nodejs:
        flat["TOOLCHAINS_NODEJS_MAX_OLD_SPACE_MB"] = nodejs["max_old_space_mb"]

    return flat


class YamlConfigSource(YamlConfigSettingsSource):
    """Reads config.yaml and flattens it into Settings' env-style aliases.

    Subclasses pydantic-settings' own YamlConfigSettingsSource (reusing its
    file-reading, so a missing file behaves exactly like "no YAML" and every
    field falls back to its Python default — identical to today's
    hardcoded-constant behavior) but flattens the nested structure before
    handing values to pydantic, since Settings stays flat (see module
    docstring in flatten_yaml_config). Path is resolved relative to the
    package (R5.1.4), not the process CWD.
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        yaml_file: Path | str | None,
    ) -> None:
        self.yaml_file_path = yaml_file
        self.yaml_file_encoding = "utf-8"
        self.yaml_config_section = None
        raw = self._read_files(self.yaml_file_path, deep_merge=False)
        InitSettingsSource.__init__(self, settings_cls, flatten_yaml_config(raw))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_DEFAULT_ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=_DEFAULT_CONFIG_FILE,
        # PROVIDERS__<ID>__<FIELD> -> providers.<id>.<field> (PRD §3.5).
        env_nested_delimiter="__",
    )

    # --- App ---
    log_level: str = Field(default="info", validation_alias="LOG_LEVEL")
    data_dir: str = Field(default="./data", validation_alias="DATA_DIR")

    # The agent's display name: comment headers, logs, the bot-login
    # fallback. Not the provider's — see `providers`.
    agent_name: str = Field(default="Issue Agent", validation_alias="AGENT_NAME")

    # --- Provider instances (PRD §3.5) ---
    # `providers.<id>` from config.yaml, deep-merged with
    # PROVIDERS__<ID>__<FIELD> env vars (where the secrets live).
    providers: dict[str, ProviderSettings] = Field(
        default_factory=dict, validation_alias="PROVIDERS"
    )

    # --- LLM (via LiteLLM) ---
    llm_api_key: str = Field(validation_alias="LLM_API_KEY")
    llm_model: str = Field(
        default="anthropic/claude-sonnet-4-5", validation_alias="LLM_MODEL"
    )
    llm_base_url: str = Field(default="", validation_alias="LLM_BASE_URL")
    llm_max_retries: int = Field(default=5, validation_alias="LLM_MAX_RETRIES")
    # Per-request timeout handed to LiteLLM (`timeout`). Without one a
    # half-dead connection (provider drop, silent SSL EOF) can hang a call
    # until the whole task_timeout expires, instead of failing fast into the
    # retry layers. 0 disables (LiteLLM's own default). Global only — not
    # part of the per-repo `llm:` override.
    llm_request_timeout_seconds: int = Field(
        default=120, ge=0, validation_alias="LLM_REQUEST_TIMEOUT_SECONDS"
    )
    # Explicit LiteLLM provider (R5.5.1) — passed as `custom_llm_provider` and
    # used for the function-calling capability check. Empty = derive from the
    # model prefix via litellm.get_llm_provider(). Never inferred from the key.
    llm_provider: str = Field(default="", validation_alias="LLM_PROVIDER")
    # strict: abort a task when a step's cost can't be determined (default —
    # a masked 0.0 is exactly how the budget guards failed in production).
    # lenient: log and carry on with 0.0 (the pre-R5.5 behavior).
    llm_cost_tracking: Literal["strict", "lenient"] = Field(
        default="strict", validation_alias="LLM_COST_TRACKING"
    )
    # default_end: Anthropic-style cache-control markers on the last message.
    # none: send no cache markers (providers that reject the field).
    llm_prompt_caching: Literal["default_end", "none"] = Field(
        default="default_end", validation_alias="LLM_PROMPT_CACHING"
    )
    llm_temperature: float | None = Field(
        default=None, validation_alias="LLM_TEMPERATURE"
    )
    llm_max_output_tokens: int | None = Field(
        default=None, validation_alias="LLM_MAX_OUTPUT_TOKENS"
    )
    # LiteLLM maps this to the provider's native reasoning shape (see
    # runner/agent_factory.py's _model_kwargs). On adaptive-thinking Anthropic
    # models (Opus 4.6+, Sonnet 4.6+) it becomes thinking=adaptive +
    # output_config.effort; on older models (including this file's default
    # model) it becomes a FIXED thinking budget instead, and that budget must
    # stay below llm_max_output_tokens or the provider 400s. A model with no
    # reasoning support raises UnsupportedParamsError, which aborts the task
    # immediately rather than silently ignoring the setting.
    llm_reasoning_effort: (
        Literal["minimal", "low", "medium", "high", "xhigh", "max", "none"] | None
    ) = Field(default=None, validation_alias="LLM_REASONING_EFFORT")
    # Cap on a single command observation before it is elided head/tail
    # (upstream's own value in minisweagent/config/default.yaml; the Python
    # constructor defaults to no cap at all). An uncapped `pytest -v` enters
    # the history whole and is re-sent on every later step, up to
    # ContextWindowExceededError — which aborts the run. 0 disables the cap.
    llm_observation_max_chars: int = Field(
        default=10000, validation_alias="LLM_OBSERVATION_MAX_CHARS"
    )
    # Optional failover: LiteLLM retries this model if the primary errors
    # (R5.5). Its key is a secret and stays in env (LLM_FALLBACK_API_KEY),
    # never in config.yaml; empty falls back to LLM_API_KEY.
    llm_fallback_model: str = Field(
        default="", validation_alias="LLM_FALLBACK_MODEL"
    )
    llm_fallback_base_url: str = Field(
        default="", validation_alias="LLM_FALLBACK_BASE_URL"
    )
    llm_fallback_api_key: str = Field(
        default="", validation_alias="LLM_FALLBACK_API_KEY"
    )

    # --- Budgets ---
    max_iterations: int = Field(default=50, validation_alias="MAX_ITERATIONS")
    max_budget_per_task_usd: float = Field(
        default=2.00, validation_alias="MAX_BUDGET_PER_TASK_USD"
    )
    max_daily_budget_usd: float = Field(
        default=50.00, validation_alias="MAX_DAILY_BUDGET_USD"
    )
    task_timeout_seconds: int = Field(
        default=900, validation_alias="TASK_TIMEOUT_SECONDS"
    )
    command_timeout_seconds: int = Field(
        default=120, validation_alias="COMMAND_TIMEOUT_SECONDS"
    )
    max_task_retries: int = Field(default=3, validation_alias="MAX_TASK_RETRIES")
    # Percentage of any single limit (iterations, cost, wall time) past which
    # each observation carries a <budget_status> block telling the agent to
    # wind down — what makes prompts/system.md's "at 80% of budget, leave the
    # best partial edit" instruction executable. 0 disables the block.
    budget_warn_at_percent: int = Field(
        default=80, validation_alias="BUDGET_WARN_AT_PERCENT"
    )

    # --- Worker ---
    poll_interval_seconds: float = Field(
        default=2.0, validation_alias="POLL_INTERVAL_SECONDS"
    )
    orphan_timeout_seconds: int = Field(
        default=90, validation_alias="ORPHAN_TIMEOUT_SECONDS"
    )
    backoff_schedule_seconds: list[int] = Field(
        default_factory=lambda: [60, 300, 1800],
        validation_alias="BACKOFF_SCHEDULE_SECONDS",
    )

    # --- Repos ---
    # Structured list of (issue-repo, code-repo) pairs (R5.2/R5.3).
    # Fed only by config.yaml's `repos:` (via flatten_yaml_config's
    # REPO_ROUTES) — never by an isolated env var, since it's a list of
    # structured objects, not a scalar. D3 of the PRD fixes this list as
    # static (no hot-reload / no DB-backed route table).
    repo_routes: list[RepoRoute] = Field(
        default_factory=list, validation_alias="REPO_ROUTES"
    )

    @computed_field
    @property
    def allowed_repos(self) -> list[str]:
        return [r.issues for r in self.repo_routes]

    @model_validator(mode="after")
    def _validate_provider_instances(self) -> Settings:
        """Instance ids are path- and env-safe; every route names a
        configured instance (implicitly, when there is only one), and no
        issues repo is routed twice on the same instance."""
        for instance_id in self.providers:
            if not _INSTANCE_ID_RE.match(instance_id):
                raise ValueError(
                    f"provider instance id {instance_id!r} must be lowercase "
                    "letters/digits separated by single '-' or '_'"
                )
        seen: set[tuple[str, str]] = set()
        for route in self.repo_routes:
            if route.provider is None:
                if len(self.providers) != 1:
                    raise ValueError(
                        f"repos[] entry {route.issues!r} needs `provider:` "
                        f"(configured instances: {sorted(self.providers) or 'none'})"
                    )
                route.provider = next(iter(self.providers))
            elif route.provider not in self.providers:
                raise ValueError(
                    f"repos[] entry {route.issues!r} names provider "
                    f"{route.provider!r}, which is not configured"
                )
            key = (route.provider, route.issues)
            if key in seen:
                raise ValueError(
                    f"repos[] routes {route.issues!r} twice on provider {route.provider!r}"
                )
            seen.add(key)
        return self

    def route_for(self, provider_instance: str, issue_repo: str) -> RepoRoute | None:
        """Resolve the RepoRoute configured for the repo an issue came from,
        on the provider instance it came from."""
        for route in self.repo_routes:
            if route.provider == provider_instance and route.issues == issue_repo:
                return route
        return None

    def llm_profile(self, route: RepoRoute | None = None) -> LlmProfile:
        """Resolve the LLM config for a task: the global `llm:` block with any
        field present in `route.llm` overlaid on top, field by field.

        "Present" means present in `RepoLlmOverride.model_fields_set` — not
        "resolved value is not None" — so an explicit `null` in YAML can
        reset `temperature`/`max_output_tokens` back to "let the provider
        decide", or `fallback: null` can disable failover for one repo, even
        though the global value is non-null. `route=None` (or a route with no
        `llm:` block) returns the global config unchanged.

        `model_fields_set` does not survive a `model_dump()` /
        `model_validate()` round-trip (it's empty after reconstruction) —
        nothing in this codebase does that to a `RepoRoute` today, but a
        future caller that serializes and rebuilds one would silently lose
        every override.

        API keys are never read from `route.llm` (RepoLlmOverride has no
        `api_key` field) — they always come from the global Settings fields.
        """
        override = route.llm if route is not None else None
        fields = override.model_fields_set if override is not None else set()

        def pick(name: str, global_value):
            return getattr(override, name) if name in fields else global_value

        fallback_override = override.fallback if override is not None else None
        if override is not None and "fallback" in fields and fallback_override is None:
            # `fallback: null` — explicit "no failover for this repo".
            fallback_model = ""
            fallback_base_url = ""
        elif fallback_override is not None:
            fallback_fields = fallback_override.model_fields_set
            fallback_model = (
                fallback_override.model
                if "model" in fallback_fields
                else self.llm_fallback_model
            )
            fallback_base_url = (
                fallback_override.base_url
                if "base_url" in fallback_fields
                else self.llm_fallback_base_url
            )
        else:
            fallback_model = self.llm_fallback_model
            fallback_base_url = self.llm_fallback_base_url

        return LlmProfile(
            model=pick("model", self.llm_model),
            provider=pick("provider", self.llm_provider),
            base_url=pick("base_url", self.llm_base_url),
            max_retries=pick("max_retries", self.llm_max_retries),
            cost_tracking=pick("cost_tracking", self.llm_cost_tracking),
            prompt_caching=pick("prompt_caching", self.llm_prompt_caching),
            temperature=pick("temperature", self.llm_temperature),
            max_output_tokens=pick("max_output_tokens", self.llm_max_output_tokens),
            reasoning_effort=pick("reasoning_effort", self.llm_reasoning_effort),
            observation_max_chars=pick(
                "observation_max_chars", self.llm_observation_max_chars
            ),
            fallback_model=fallback_model,
            fallback_base_url=fallback_base_url,
            # Always global — RepoLlmOverride has no api_key field.
            api_key=self.llm_api_key,
            fallback_api_key=self.llm_fallback_api_key,
        )

    # --- PR ---
    pr_slug_max_length: int = Field(default=30, validation_alias="PR_SLUG_MAX_LENGTH")
    pr_label_agent_generated: str = Field(
        default="agent-generated", validation_alias="PR_LABEL_AGENT_GENERATED"
    )

    # --- Git identity ---
    # How much history the workspace clone carries. 0 (default) clones the
    # full history, which is what lets the agent answer "when did this land"
    # with `git log -S` before concluding an issue is already fixed; a
    # positive value becomes `git clone --depth=N`, bounding clone time and
    # volume space on large repos at the cost of that lookup.
    git_clone_depth: int = Field(default=0, validation_alias="GIT_CLONE_DEPTH")
    git_user_name: str = Field(
        default="issue-agent[bot]", validation_alias="GIT_USER_NAME"
    )
    # Empty = the provider's default for `git_user_name`
    # (GitTransport.default_commit_email).
    git_user_email: str = Field(default="", validation_alias="GIT_USER_EMAIL")

    # --- Metrics ---
    metrics_port: int = Field(default=9090, validation_alias="METRICS_PORT")
    metrics_task_duration_buckets: list[float] = Field(
        default_factory=lambda: [1, 5, 10, 30, 60, 120, 300, 600],
        validation_alias="METRICS_TASK_DURATION_BUCKETS",
    )
    metrics_iteration_buckets: list[float] = Field(
        default_factory=lambda: [1, 5, 10, 20, 50, 100],
        validation_alias="METRICS_ITERATION_BUCKETS",
    )

    # --- Prompts ---
    prompt_category_map: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_CATEGORY_PROMPT_MAP),
        validation_alias="PROMPT_CATEGORY_MAP",
    )

    # --- Policy (label -> action routing) ---
    # Which prompt categories route to LabelAction.TRIAGE_ONLY / DIAGNOSE
    # (policy/label_router.py's classify_labels()). Defaults
    # reproduce the behaviour that was hardcoded before this became
    # configurable. Categories not listed in either key keep their built-in
    # action (bug/docs/chore/agent_fix_please -> FIX; unrecognized -> SKIP).
    policy_triage_only_categories: list[str] = Field(
        default_factory=lambda: sorted(DEFAULT_TRIAGE_ONLY_CATEGORIES),
        validation_alias="POLICY_TRIAGE_ONLY_CATEGORIES",
    )
    policy_diagnose_categories: list[str] = Field(
        default_factory=lambda: sorted(DEFAULT_DIAGNOSE_CATEGORIES),
        validation_alias="POLICY_DIAGNOSE_CATEGORIES",
    )

    @model_validator(mode="after")
    def _validate_policy_categories(self) -> Settings:
        for field_name, categories in (
            ("policy_triage_only_categories", self.policy_triage_only_categories),
            ("policy_diagnose_categories", self.policy_diagnose_categories),
        ):
            unknown = set(categories) - ALL_CATEGORIES
            if unknown:
                raise ValueError(
                    f"{field_name} contains unknown categor{'y' if len(unknown) == 1 else 'ies'} "
                    f"{sorted(unknown)!r} — must be a subset of {sorted(ALL_CATEGORIES)!r}"
                )
        overlap = set(self.policy_triage_only_categories) & set(
            self.policy_diagnose_categories
        )
        if overlap:
            raise ValueError(
                "policy_triage_only_categories and policy_diagnose_categories "
                f"must not overlap — both list {sorted(overlap)!r}"
            )
        return self

    # --- Comments ---
    # R1.6: post a comment when the agent starts (or resumes) work on an issue.
    comments_initial_comment: bool = Field(
        default=True, validation_alias="COMMENTS_INITIAL_COMMENT"
    )

    # R6: post a "still working" comment every N agent iterations (0 = off),
    # capped at `progress_max_per_run` comments per execution.
    comments_progress_every_iterations: int = Field(
        default=20, validation_alias="COMMENTS_PROGRESS_EVERY_ITERATIONS"
    )
    comments_progress_max_per_run: int = Field(
        default=2, validation_alias="COMMENTS_PROGRESS_MAX_PER_RUN"
    )

    # R2: which comment authors may trigger a run. A rejected comment is
    # dropped silently at ingestion — no task, no GitHub comment, no cost.
    comments_author_filter_enabled: bool = Field(
        default=True, validation_alias="COMMENTS_AUTHOR_FILTER_ENABLED"
    )
    # R2.1: the issue's own author always passes — matched by login, never by
    # account type, so a bot that opened the issue keeps its follow-up channel
    # for as long as the issue is open (a maintainer may reopen it).
    comments_author_filter_allow_issue_author: bool = Field(
        default=True, validation_alias="COMMENTS_AUTHOR_FILTER_ALLOW_ISSUE_AUTHOR"
    )
    comments_author_filter_allow_associations: list[str] = Field(
        default_factory=lambda: ["OWNER", "MEMBER", "COLLABORATOR"],
        validation_alias="COMMENTS_AUTHOR_FILTER_ALLOW_ASSOCIATIONS",
    )
    # R2.6: logins cleared individually, bots included.
    comments_author_filter_allowlist: list[str] = Field(
        default_factory=list,
        validation_alias="COMMENTS_AUTHOR_FILTER_ALLOWLIST",
    )

    # R3: how much of the discussion thread reaches the model.
    comments_context_max_comments: int = Field(
        default=20, validation_alias="COMMENTS_CONTEXT_MAX_COMMENTS"
    )
    comments_context_max_chars_per_comment: int = Field(
        default=2000, validation_alias="COMMENTS_CONTEXT_MAX_CHARS_PER_COMMENT"
    )

    # --- Resume ---
    # R4.8: false = today's behavior — nothing preserved on a block, nothing
    # resumed. `issue_work` is still maintained (R1/R6/R7 depend on it).
    resume_enabled: bool = Field(default=True, validation_alias="RESUME_ENABLED")
    # R4.3: commit and push the partial work to the work branch before cleanup.
    resume_push_wip_on_block: bool = Field(
        default=True, validation_alias="RESUME_PUSH_WIP_ON_BLOCK"
    )
    # R4.7: attempts before the agent gives up and closes with `max_attempts`.
    resume_max_attempts: int = Field(
        default=3, validation_alias="RESUME_MAX_ATTEMPTS"
    )
    # R5.2: never | mechanical | llm_unless_budget | llm. `mechanical` (default)
    # costs zero tokens — it reads the trajectory the agent already produced.
    resume_summary_on_block: Literal[
        "never", "mechanical", "llm_unless_budget", "llm"
    ] = Field(default="mechanical", validation_alias="RESUME_SUMMARY_ON_BLOCK")
    # R5.5: block summary is truncated to this before it goes to the DB / prompt.
    resume_summary_max_chars: int = Field(
        default=4000, validation_alias="RESUME_SUMMARY_MAX_CHARS"
    )
    # Bounded, in-task auto-retry when the harness's own post-push test
    # verification fails: reuses resume_max_attempts (the SAME cap the
    # block-retry machinery uses, no parallel counter) and re-checks the
    # daily budget before each retry. false = today's behavior — the PR
    # opens regardless of the test result, no retry.
    resume_retry_on_test_failure: bool = Field(
        default=True, validation_alias="RESUME_RETRY_ON_TEST_FAILURE"
    )
    # R7.3/R7.4: the reconciliation sweep — how often the worker checks whether
    # any open issue_work's issue was closed behind its back, and the hard cap
    # on GitHub issue lookups per sweep.
    resume_reconcile_interval_minutes: int = Field(
        default=60, validation_alias="RESUME_RECONCILE_INTERVAL_MINUTES"
    )
    resume_reconcile_max_per_cycle: int = Field(
        default=50, validation_alias="RESUME_RECONCILE_MAX_PER_CYCLE"
    )

    # --- Alerts ---
    alert_webhook_url: str = Field(default="", validation_alias="ALERT_WEBHOOK_URL")

    # --- Retention & Cleanup ---
    events_retention_days: int = Field(
        default=30, validation_alias="EVENTS_RETENTION_DAYS"
    )
    db_retention_days: int = Field(
        default=90, validation_alias="DB_RETENTION_DAYS"
    )
    audit_retention_days: int = Field(
        default=90, validation_alias="AUDIT_RETENTION_DAYS"
    )
    cleanup_interval_minutes: int = Field(
        default=60, validation_alias="CLEANUP_INTERVAL_MINUTES"
    )

    # --- Toolchains (plugins install runtimes on demand into /data/tools) ---
    toolchains_enabled: bool = Field(default=True, validation_alias="TOOLCHAINS_ENABLED")
    toolchains_tools_root: Path = Field(
        default=Path("/data/tools"), validation_alias="TOOLCHAINS_TOOLS_ROOT"
    )
    toolchains_nodejs_default_version: str = Field(
        default="20", validation_alias="TOOLCHAINS_NODEJS_DEFAULT_VERSION"
    )
    toolchains_nodejs_mirror: str = Field(
        default="https://nodejs.org/dist", validation_alias="TOOLCHAINS_NODEJS_MIRROR"
    )
    # V8 old-space cap for every Node process the harness or the agent starts
    # (tsc, jest, npm), enforced by a `node` shim (runner/guards.py): a share of
    # the VM's MemTotal, so it follows the machine if it is resized. An explicit
    # MB value wins over the percentage; 0 for both disables the cap.
    toolchains_nodejs_max_old_space_percent: int = Field(
        default=40, ge=0, le=100, validation_alias="TOOLCHAINS_NODEJS_MAX_OLD_SPACE_PERCENT"
    )
    toolchains_nodejs_max_old_space_mb: int = Field(
        default=0, ge=0, validation_alias="TOOLCHAINS_NODEJS_MAX_OLD_SPACE_MB"
    )
    toolchains_install_timeout_seconds: int = Field(
        default=300, validation_alias="TOOLCHAINS_INSTALL_TIMEOUT_SECONDS"
    )
    toolchains_lock_timeout_seconds: int = Field(
        default=600, validation_alias="TOOLCHAINS_LOCK_TIMEOUT_SECONDS"
    )

    # --- Author allowlist (opcional) ---
    allowed_authors_raw: str = Field(
        default="",
        validation_alias="ALLOWED_AUTHORS",
    )

    @computed_field
    @property
    def allowed_authors(self) -> list[str]:
        return [a.strip() for a in self.allowed_authors_raw.split(",") if a.strip()]

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        config_file_env = os.environ.get("CONFIG_FILE")
        if config_file_env:
            yaml_file: Path | None = Path(config_file_env)
        else:
            yaml_file = settings_cls.model_config.get("yaml_file")
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSource(settings_cls, yaml_file=yaml_file),
            file_secret_settings,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
