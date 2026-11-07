from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Union

import pytest
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, InitSettingsSource, SettingsConfigDict

from issue_agent.config import RepoRoute, Settings, get_settings
from tests.helpers.settings import github_providers


class SettingsDotenv(Settings):
    """Reads .env from the repo root (inherited from Settings)."""

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_CONFIG_YAML_PATH = Path(__file__).resolve().parents[2] / "config.yaml"


@pytest.mark.skipif(not _ENV_PATH.exists(), reason=".env not found — CI environment")
class TestLoadFromEnvFile:
    """Dev scenario: .env exists at repo root."""

    def teardown_method(self):
        get_settings.cache_clear()

    def test_github_app_id(self):
        get_settings.cache_clear()
        s = get_settings()
        assert s.providers["github"].app_id == 3735121

    def test_log_level_dev_override(self):
        get_settings.cache_clear()
        s = get_settings()
        assert s.log_level == "debug"

    def test_budget_dev_override(self):
        get_settings.cache_clear()
        s = get_settings()
        assert s.max_iterations == 10
        assert s.max_budget_per_task_usd == 0.50
        assert s.max_daily_budget_usd == 5.0
        assert s.task_timeout_seconds == 300

    @pytest.mark.skipif(
        not _CONFIG_YAML_PATH.exists(),
        reason="no local config.yaml (cp config.yaml.example config.yaml)",
    )
    def test_repo_routes_loaded_from_config_yaml(self):
        # repo_routes comes only from config.yaml's `repos:` (static
        # list, YAML-only). This just proves get_settings() resolves the
        # local config.yaml's repos list rather than raising/being empty.
        get_settings.cache_clear()
        s = get_settings()
        assert len(s.repo_routes) >= 1
        first_issues = s.repo_routes[0].issues
        assert s.allowed_repos == [r.issues for r in s.repo_routes]
        assert s.route_for(s.repo_routes[0].provider, first_issues) is not None
        assert s.route_for("github", "definitely/not-configured") is None

    def test_private_key_loaded_from_path(self):
        get_settings.cache_clear()
        s = get_settings()
        key = s.providers["github"].app_private_key
        assert key.startswith("-----BEGIN RSA PRIVATE KEY-----")
        assert len(key) > 200


class TestLoadFromDotenv:
    """Settings com .env — valores reais da raiz do repo, kwargs sobrescrevem."""

    def _make(self, **overrides) -> Settings:
        defaults = dict(
            PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
            LLM_API_KEY="k",
        )
        defaults.update(overrides)
        return SettingsDotenv(**defaults)

    @pytest.mark.skipif(not _ENV_PATH.exists(), reason=".env not found — CI environment")
    def test_dotenv_overrides_defaults(self):
        s = self._make()
        assert s.max_iterations == 10
        assert s.max_budget_per_task_usd == 0.50
        assert s.max_daily_budget_usd == 5.0
        assert s.task_timeout_seconds == 300
        assert s.log_level == "debug"

    def test_field_defaults_preserved_when_not_in_dotenv(self):
        s = self._make()
        assert s.llm_max_retries == 5
        assert s.llm_request_timeout_seconds == 120
        assert s.agent_name == "Issue Agent"
        assert s.providers["github"].bot_login == ""

    def test_bot_login_from_kwarg(self):
        s = self._make(PROVIDERS=github_providers(bot_login="stateway-issue-agent[bot]"))
        assert s.providers["github"].bot_login == "stateway-issue-agent[bot]"

    def test_kwargs_override_dotenv(self):
        s = self._make(MAX_ITERATIONS="99", MAX_BUDGET_PER_TASK_USD="3.00")
        assert s.max_iterations == 99
        assert s.max_budget_per_task_usd == 3.00

    def test_repo_routes_from_init_kwarg(self):
        # repo_routes is fed only by YAML/init kwargs, never a scalar env var —
        # SettingsDotenv has yaml_file=None, so it stays empty unless passed
        # explicitly via the REPO_ROUTES init kwarg (validation_alias).
        s = self._make(REPO_ROUTES=[{"issues": "a/b"}, {"issues": "c/d"}, {"issues": "e/f"}])
        assert s.allowed_repos == ["a/b", "c/d", "e/f"]

    def test_private_key_inline(self):
        s = self._make(PROVIDERS=github_providers(app_private_key="inline-value"))
        assert s.providers["github"].app_private_key == "inline-value"

    def test_private_key_loaded_from_path(self, tmp_path):
        key = tmp_path / "app.pem"
        key.write_text("-----BEGIN RSA PRIVATE KEY-----\nfrom-file\n")
        s = self._make(
            PROVIDERS=github_providers(app_private_key="", app_private_key_path=str(key))
        )
        assert s.providers["github"].app_private_key.endswith("from-file\n")

    def test_legacy_env_vars_are_ignored(self):
        # Hard break (PRD §3.5): the old names configure nothing.
        s = self._make(GITHUB_INSTALLATION_ID=12345, GITHUB_APP_ID=7)
        assert not hasattr(s, "github_installation_id")
        assert not hasattr(s, "github_app_id")


class TestSingleton:
    @pytest.mark.skipif(not _ENV_PATH.exists(), reason=".env not found — CI environment")
    def test_get_settings_cached(self):
        get_settings.cache_clear()
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    @pytest.mark.skipif(not _ENV_PATH.exists(), reason=".env not found — CI environment")
    def test_cache_clear(self):
        get_settings.cache_clear()
        s1 = get_settings()
        get_settings.cache_clear()
        s2 = get_settings()
        assert s1 is not s2


class TestSerialization:
    def test_model_dump(self):
        s = SettingsDotenv(
            PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
            LLM_API_KEY="k",
            REPO_ROUTES=[{"issues": "a/b"}, {"issues": "c/d"}],
        )
        d = s.model_dump()
        assert d["allowed_repos"] == ["a/b", "c/d"]
        assert d["providers"]["github"]["app_id"] == 1

    def test_model_dump_json(self):
        s = SettingsDotenv(
            PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
            LLM_API_KEY="k",
            REPO_ROUTES=[{"issues": "owner/repo"}],
        )
        j = s.model_dump_json()
        parsed = json.loads(j)
        assert parsed["providers"]["github"]["app_id"] == 1
        assert isinstance(parsed["allowed_repos"], list)

    def test_model_dump_includes_repo_routes(self):
        # repo_routes (list[RepoRoute]) is a real Settings field, so it must
        # round-trip through model_dump() with every nested RepoRoute/
        # ProjectProfile value intact — anything that serializes Settings
        # (logging, observability) keeps seeing the full per-repo config
        # (the old single-repo shim fields were removed with multi-repo routing).
        s = SettingsDotenv(
            PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
            LLM_API_KEY="k",
            REPO_ROUTES=[
                {
                    "issues": "owner/repo",
                    "target": "owner/repo-code",
                    "base_branch": "dev",
                    "project": {
                        "test_command": "pytest",
                        "lint_command": "ruff check",
                        "protected_paths": ["migrations/"],
                        "relevant_extensions": [".py"],
                    },
                }
            ],
        )
        d = s.model_dump()
        route = d["repo_routes"][0]
        assert route["target"] == "owner/repo-code"
        assert route["base_branch"] == "dev"
        assert route["project"]["test_command"] == "pytest"
        assert route["project"]["lint_command"] == "ruff check"
        assert route["project"]["protected_paths"] == ["migrations/"]
        assert route["project"]["relevant_extensions"] == [".py"]

    def test_model_dump_repo_routes_empty_when_no_routes(self):
        s = SettingsDotenv(
            PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
            LLM_API_KEY="k",
        )
        d = s.model_dump()
        assert d["repo_routes"] == []
        assert d["allowed_repos"] == []


class TestTypes:
    def test_int_fields(self):
        s = SettingsDotenv(
            PROVIDERS=github_providers(app_id="42", app_private_key="k", webhook_secret="s"),
            LLM_API_KEY="k",
            MAX_ITERATIONS="99",
            TASK_TIMEOUT_SECONDS="600",
        )
        assert isinstance(s.providers["github"].app_id, int)
        assert isinstance(s.max_iterations, int)
        assert isinstance(s.task_timeout_seconds, int)
        assert s.max_iterations == 99
        assert s.task_timeout_seconds == 600

    def test_float_fields(self):
        s = SettingsDotenv(
            PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
            LLM_API_KEY="k",
            MAX_BUDGET_PER_TASK_USD="1.50",
        )
        assert isinstance(s.max_budget_per_task_usd, float)
        assert s.max_budget_per_task_usd == 1.50


class TestLlmConfigFields:
    """Provider, cost_tracking, prompt_caching, sampling and the
    optional failover are all parameters, none inferred from the API key."""

    def _settings(self, **overrides) -> Settings:
        base = dict(
            PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
            LLM_API_KEY="k",
        )
        base.update(overrides)
        return SettingsDotenv(**base)

    def test_defaults(self):
        s = self._settings()
        assert s.llm_provider == ""
        assert s.llm_cost_tracking == "strict"
        assert s.llm_prompt_caching == "default_end"
        assert s.llm_temperature is None
        assert s.llm_max_output_tokens is None
        assert s.llm_reasoning_effort is None
        assert s.llm_fallback_model == ""
        assert s.llm_fallback_base_url == ""
        assert s.llm_fallback_api_key == ""

    def test_env_overrides(self):
        s = self._settings(
            LLM_PROVIDER="openrouter",
            LLM_COST_TRACKING="lenient",
            LLM_PROMPT_CACHING="none",
            LLM_TEMPERATURE="0.2",
            LLM_MAX_OUTPUT_TOKENS="4096",
            LLM_REASONING_EFFORT="high",
            LLM_FALLBACK_MODEL="openrouter/anthropic/claude-sonnet-4-5",
            LLM_FALLBACK_BASE_URL="https://proxy.example",
            LLM_FALLBACK_API_KEY="sk-fallback",
        )
        assert s.llm_provider == "openrouter"
        assert s.llm_cost_tracking == "lenient"
        assert s.llm_prompt_caching == "none"
        assert s.llm_temperature == 0.2
        assert s.llm_max_output_tokens == 4096
        assert s.llm_reasoning_effort == "high"
        assert s.llm_fallback_model == "openrouter/anthropic/claude-sonnet-4-5"
        assert s.llm_fallback_base_url == "https://proxy.example"
        assert s.llm_fallback_api_key == "sk-fallback"

    def test_cost_tracking_rejects_unknown_value(self):
        with pytest.raises(ValidationError):
            self._settings(LLM_COST_TRACKING="loose")

    def test_prompt_caching_rejects_unknown_value(self):
        with pytest.raises(ValidationError):
            self._settings(LLM_PROMPT_CACHING="aggressive")

    def test_reasoning_effort_rejects_unknown_value(self):
        with pytest.raises(ValidationError):
            self._settings(LLM_REASONING_EFFORT="ultra")

    def test_llm_block_flattened_from_yaml(self):
        from issue_agent.config import flatten_yaml_config

        raw = {
            "llm": {
                "provider": "openrouter",
                "cost_tracking": "lenient",
                "prompt_caching": "none",
                "temperature": 0.1,
                "max_output_tokens": 2048,
                "reasoning_effort": "xhigh",
                "fallback": {
                    "model": "openrouter/x/y",
                    "base_url": "https://proxy.example",
                },
            }
        }
        flat = flatten_yaml_config(raw)
        assert flat["LLM_PROVIDER"] == "openrouter"
        assert flat["LLM_COST_TRACKING"] == "lenient"
        assert flat["LLM_PROMPT_CACHING"] == "none"
        assert flat["LLM_TEMPERATURE"] == 0.1
        assert flat["LLM_MAX_OUTPUT_TOKENS"] == 2048
        assert flat["LLM_REASONING_EFFORT"] == "xhigh"
        assert flat["LLM_FALLBACK_MODEL"] == "openrouter/x/y"
        assert flat["LLM_FALLBACK_BASE_URL"] == "https://proxy.example"

    def test_providers_pass_through_from_yaml(self):
        from issue_agent.config import flatten_yaml_config

        providers = {"github": {"type": "github", "app_id": 1, "bot_login": "agent[bot]"}}
        flat = flatten_yaml_config({"providers": providers})
        assert flat["PROVIDERS"] == providers

    def test_observation_max_chars_flattened_from_yaml(self):
        from issue_agent.config import flatten_yaml_config

        flat = flatten_yaml_config({"llm": {"observation_max_chars": 4000}})
        assert flat["LLM_OBSERVATION_MAX_CHARS"] == 4000

    def test_budget_warn_at_percent_flattened_from_yaml(self):
        from issue_agent.config import flatten_yaml_config

        flat = flatten_yaml_config({"budgets": {"warn_at_percent": 60}})
        assert flat["BUDGET_WARN_AT_PERCENT"] == 60

    def test_llm_fallback_api_key_never_flattened_from_yaml(self):
        from issue_agent.config import flatten_yaml_config

        flat = flatten_yaml_config({"llm": {"fallback": {"api_key": "sk-leaked"}}})
        assert "LLM_FALLBACK_API_KEY" not in flat

    def test_toolchains_block_flattened_from_yaml(self):
        from issue_agent.config import flatten_yaml_config

        raw = {
            "toolchains": {
                "enabled": False,
                "tools_root": "/custom/tools",
                "install_timeout_seconds": 999,
                "lock_timeout_seconds": 888,
                "nodejs": {
                    "default_version": "18",
                    "mirror": "https://internal-mirror.example/dist",
                },
            }
        }
        flat = flatten_yaml_config(raw)
        assert flat["TOOLCHAINS_ENABLED"] is False
        assert flat["TOOLCHAINS_TOOLS_ROOT"] == "/custom/tools"
        assert flat["TOOLCHAINS_INSTALL_TIMEOUT_SECONDS"] == 999
        assert flat["TOOLCHAINS_LOCK_TIMEOUT_SECONDS"] == 888
        assert flat["TOOLCHAINS_NODEJS_DEFAULT_VERSION"] == "18"
        assert flat["TOOLCHAINS_NODEJS_MIRROR"] == "https://internal-mirror.example/dist"

    def test_toolchains_block_absent_from_yaml_flattens_to_nothing(self):
        from issue_agent.config import flatten_yaml_config

        flat = flatten_yaml_config({})
        assert not any(k.startswith("TOOLCHAINS_") for k in flat)

    def test_yaml_supplies_llm_block_env_still_wins(self, tmp_path, monkeypatch):
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text("llm:\n  cost_tracking: lenient\n  temperature: 0.5\n")
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        monkeypatch.setenv("LLM_COST_TRACKING", "strict")
        s = _SettingsIsolated(
            PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
            LLM_API_KEY="k",
        )
        assert s.llm_cost_tracking == "strict"  # env wins
        assert s.llm_temperature == 0.5  # yaml supplies


class TestCommentsConfig:
    """`comments` block, starting with `comments.initial_comment`."""

    def _settings(self, **overrides) -> Settings:
        base = dict(
            PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
            LLM_API_KEY="k",
        )
        base.update(overrides)
        return SettingsDotenv(**base)

    def test_initial_comment_defaults_true(self):
        assert self._settings().comments_initial_comment is True

    def test_initial_comment_env_override(self):
        assert self._settings(COMMENTS_INITIAL_COMMENT="false").comments_initial_comment is False

    def test_initial_comment_flattened_from_yaml(self):
        from issue_agent.config import flatten_yaml_config

        flat = flatten_yaml_config({"comments": {"initial_comment": False}})
        assert flat["COMMENTS_INITIAL_COMMENT"] is False

    def test_comments_absent_yields_no_key(self):
        from issue_agent.config import flatten_yaml_config

        assert "COMMENTS_INITIAL_COMMENT" not in flatten_yaml_config({})

    # --- comments.author_filter / comments.context ---

    def test_author_filter_defaults(self):
        s = self._settings()
        assert s.comments_author_filter_enabled is True
        assert s.comments_author_filter_allow_issue_author is True
        assert s.comments_author_filter_allow_associations == [
            "OWNER",
            "MEMBER",
            "COLLABORATOR",
        ]
        assert s.comments_author_filter_allowlist == []

    def test_context_defaults(self):
        s = self._settings()
        assert s.comments_context_max_comments == 20
        assert s.comments_context_max_chars_per_comment == 2000

    # --- comments.progress_* ---

    def test_progress_defaults(self):
        s = self._settings()
        assert s.comments_progress_every_iterations == 20
        assert s.comments_progress_max_per_run == 2

    def test_progress_env_override(self):
        s = self._settings(
            COMMENTS_PROGRESS_EVERY_ITERATIONS="0",
            COMMENTS_PROGRESS_MAX_PER_RUN="5",
        )
        assert s.comments_progress_every_iterations == 0
        assert s.comments_progress_max_per_run == 5

    def test_progress_flattened_from_yaml(self):
        from issue_agent.config import flatten_yaml_config

        flat = flatten_yaml_config(
            {"comments": {"progress_every_iterations": 10, "progress_max_per_run": 3}}
        )
        assert flat["COMMENTS_PROGRESS_EVERY_ITERATIONS"] == 10
        assert flat["COMMENTS_PROGRESS_MAX_PER_RUN"] == 3

    def test_author_filter_enabled_env_override(self):
        assert (
            self._settings(COMMENTS_AUTHOR_FILTER_ENABLED="false").comments_author_filter_enabled
            is False
        )

    def test_author_filter_allowlist_override(self):
        assert self._settings(
            COMMENTS_AUTHOR_FILTER_ALLOWLIST=["dependabot[bot]"]
        ).comments_author_filter_allowlist == ["dependabot[bot]"]

    def test_author_filter_flattened_from_yaml(self):
        from issue_agent.config import flatten_yaml_config

        flat = flatten_yaml_config(
            {
                "comments": {
                    "author_filter": {
                        "enabled": False,
                        "allow_issue_author": False,
                        "allow_associations": ["OWNER"],
                        "allowlist": ["some-bot[bot]"],
                    },
                    "context": {
                        "max_comments": 5,
                        "max_chars_per_comment": 100,
                    },
                }
            }
        )
        assert flat["COMMENTS_AUTHOR_FILTER_ENABLED"] is False
        assert flat["COMMENTS_AUTHOR_FILTER_ALLOW_ISSUE_AUTHOR"] is False
        assert flat["COMMENTS_AUTHOR_FILTER_ALLOW_ASSOCIATIONS"] == ["OWNER"]
        assert flat["COMMENTS_AUTHOR_FILTER_ALLOWLIST"] == ["some-bot[bot]"]
        assert flat["COMMENTS_CONTEXT_MAX_COMMENTS"] == 5
        assert flat["COMMENTS_CONTEXT_MAX_CHARS_PER_COMMENT"] == 100

    def test_author_filter_absent_yields_no_keys(self):
        from issue_agent.config import flatten_yaml_config

        flat = flatten_yaml_config({"comments": {"initial_comment": True}})
        assert "COMMENTS_AUTHOR_FILTER_ENABLED" not in flat
        assert "COMMENTS_CONTEXT_MAX_COMMENTS" not in flat


class TestResumeConfig:
    """`resume` block. Governs WIP preservation
    on a block, the block summary policy, and the retry ceiling."""

    def _settings(self, **overrides) -> Settings:
        base = dict(
            PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
            LLM_API_KEY="k",
        )
        base.update(overrides)
        return SettingsDotenv(**base)

    def test_defaults(self):
        s = self._settings()
        assert s.resume_enabled is True
        assert s.resume_push_wip_on_block is True
        assert s.resume_max_attempts == 3
        assert s.resume_summary_on_block == "mechanical"
        assert s.resume_summary_max_chars == 4000
        assert s.resume_reconcile_interval_minutes == 60
        assert s.resume_reconcile_max_per_cycle == 50

    def test_enabled_env_override(self):
        assert self._settings(RESUME_ENABLED="false").resume_enabled is False

    def test_summary_policy_env_override(self):
        assert self._settings(RESUME_SUMMARY_ON_BLOCK="llm").resume_summary_on_block == "llm"

    def test_flattened_from_yaml(self):
        from issue_agent.config import flatten_yaml_config

        flat = flatten_yaml_config(
            {
                "resume": {
                    "enabled": False,
                    "push_wip_on_block": False,
                    "max_attempts": 5,
                    "summary_on_block": "llm_unless_budget",
                    "summary_max_chars": 1234,
                    "retry_on_test_failure": False,
                    "reconcile_interval_minutes": 30,
                    "reconcile_max_per_cycle": 10,
                }
            }
        )
        assert flat["RESUME_ENABLED"] is False
        assert flat["RESUME_PUSH_WIP_ON_BLOCK"] is False
        assert flat["RESUME_MAX_ATTEMPTS"] == 5
        assert flat["RESUME_SUMMARY_ON_BLOCK"] == "llm_unless_budget"
        assert flat["RESUME_SUMMARY_MAX_CHARS"] == 1234
        assert flat["RESUME_RETRY_ON_TEST_FAILURE"] is False
        assert flat["RESUME_RECONCILE_INTERVAL_MINUTES"] == 30
        assert flat["RESUME_RECONCILE_MAX_PER_CYCLE"] == 10

    def test_absent_yields_no_keys(self):
        from issue_agent.config import flatten_yaml_config

        assert "RESUME_ENABLED" not in flatten_yaml_config({})


class TestExtraIgnored:
    def test_unknown_env_vars_ignored(self):
        s = SettingsDotenv(
            PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
            LLM_API_KEY="k",
            SOME_UNKNOWN_VAR="should-not-break",
        )
        assert s.providers["github"].app_id == 1


class TestGithubHost:
    def _make(self, host: str) -> SettingsDotenv:
        return SettingsDotenv(
            PROVIDERS=github_providers(host=host),
            LLM_API_KEY="k",
        )

    @pytest.mark.parametrize(
        "raw",
        ["https://ghe.example.com", "https://ghe.example.com/", " https://ghe.example.com// "],
    )
    def test_trailing_slash_and_whitespace_are_dropped(self, raw: str) -> None:
        # The host is the prefix git matches `http.<url>.extraHeader` against
        # and is joined into clone URLs: a stray "/" breaks both.
        assert self._make(raw).providers["github"].host == "https://ghe.example.com"

    @pytest.mark.parametrize("raw", ["ghe.example.com", "git@ghe.example.com", ""])
    def test_host_without_scheme_is_rejected(self, raw: str) -> None:
        with pytest.raises(ValidationError, match="scheme"):
            self._make(raw)

    @pytest.mark.parametrize(
        ("host", "api_url"),
        [
            ("https://github.com", "https://api.github.com"),
            ("https://acme.ghe.com", "https://api.acme.ghe.com"),
            ("https://ghe.example.com", "https://ghe.example.com/api/v3"),
            ("http://ghe.internal:8080/", "http://ghe.internal:8080/api/v3"),
        ],
    )
    def test_api_url_follows_the_host(self, host: str, api_url: str) -> None:
        assert self._make(host).providers["github"].api_url == api_url


class TestFlattenYamlConfig:
    def test_repos_become_repo_routes_list(self):
        from issue_agent.config import flatten_yaml_config

        raw = {
            "repos": [
                {"issues": "a/b", "target": "a/c", "base_branch": "dev"},
                {"issues": "x/y"},
            ]
        }
        flat = flatten_yaml_config(raw)
        assert flat["REPO_ROUTES"] == raw["repos"]

    def test_repos_absent_yields_no_repo_routes_key(self):
        from issue_agent.config import flatten_yaml_config

        assert "REPO_ROUTES" not in flatten_yaml_config({})
        assert "REPO_ROUTES" not in flatten_yaml_config({"repos": []})

    def test_repos_not_prevalidated_by_flatten(self):
        # flatten_yaml_config passes raw dicts through untouched — even a
        # malformed entry (missing the required `issues` key) is forwarded
        # as-is; Settings.repo_routes is what validates it, at construction
        # time, not this function.
        from issue_agent.config import flatten_yaml_config

        raw = {"repos": [{"workflow": "not-even-checked-here"}]}
        flat = flatten_yaml_config(raw)
        assert flat["REPO_ROUTES"] == raw["repos"]

    def test_nested_sections_flattened(self):
        from issue_agent.config import flatten_yaml_config

        raw = {
            "budgets": {"max_iterations": 7},
            "worker": {"backoff_schedule_seconds": [1, 2, 3]},
            "pr": {"slug_max_length": 10, "label": "bot-pr"},
            "retention": {"events_days": 10, "db_days": 20, "audit_days": 40},
        }
        flat = flatten_yaml_config(raw)
        assert flat["MAX_ITERATIONS"] == 7
        assert flat["BACKOFF_SCHEDULE_SECONDS"] == [1, 2, 3]
        assert flat["PR_SLUG_MAX_LENGTH"] == 10
        assert flat["PR_LABEL_AGENT_GENERATED"] == "bot-pr"
        assert flat["EVENTS_RETENTION_DAYS"] == 10
        assert flat["DB_RETENTION_DAYS"] == 20
        assert flat["AUDIT_RETENTION_DAYS"] == 40

    def test_empty_raw_yields_empty_dict(self):
        from issue_agent.config import flatten_yaml_config

        assert flatten_yaml_config({}) == {}

    def test_project_block_passed_through_inside_repo_routes(self):
        # The project block is no longer flattened into scalar aliases — it
        # rides along inside each REPO_ROUTES item and Settings.repo_routes
        # (RepoRoute -> ProjectProfile) validates/defaults it later.
        from issue_agent.config import flatten_yaml_config

        raw = {
            "repos": [
                {
                    "issues": "a/b",
                    "project": {
                        "test_command": "pytest",
                        "lint_command": "ruff check",
                        "protected_paths": ["migrations/", "src/engine/"],
                        "relevant_extensions": [".py"],
                    },
                },
            ]
        }
        flat = flatten_yaml_config(raw)
        assert flat["REPO_ROUTES"][0]["project"]["test_command"] == "pytest"
        assert flat["REPO_ROUTES"][0]["project"]["lint_command"] == "ruff check"
        assert flat["REPO_ROUTES"][0]["project"]["protected_paths"] == [
            "migrations/",
            "src/engine/",
        ]
        assert flat["REPO_ROUTES"][0]["project"]["relevant_extensions"] == [".py"]


class _SettingsIsolated(Settings):
    """No real .env, no real config.yaml — isolated for precedence tests."""

    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


class TestYamlSourcePrecedence:
    """env > YAML > default, via the CONFIG_FILE override."""

    _BASE_KWARGS = dict(LLM_API_KEY="k")

    def _write_yaml(self, tmp_path: Path, contents: str) -> Path:
        p = tmp_path / "config.yaml"
        p.write_text(contents)
        return p

    def test_yaml_supplies_required_fields(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            providers:
              github:
                type: github
                app_id: 42
            repos:
              - issues: owner/repo
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        # mini-swe-agent auto-loads its own global .env on import, which may
        # set these in the real process env — force true isolation here.
        s = _SettingsIsolated(**self._BASE_KWARGS)
        assert s.providers["github"].app_id == 42
        assert s.repo_routes[0].provider == "github"
        assert s.repo_routes[0].target_repo == "owner/repo"
        assert s.allowed_repos == ["owner/repo"]

    def test_env_var_overrides_yaml(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            budgets:
              max_iterations: 5
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        monkeypatch.setenv("MAX_ITERATIONS", "77")
        s = _SettingsIsolated(
            **self._BASE_KWARGS,
        )
        assert s.max_iterations == 77

    def test_init_kwarg_overrides_yaml_and_env(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            budgets:
              max_iterations: 5
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        monkeypatch.setenv("MAX_ITERATIONS", "77")
        s = _SettingsIsolated(
            MAX_ITERATIONS="99",
            **self._BASE_KWARGS,
        )
        assert s.max_iterations == 99

    def test_yaml_supplies_toolchains_block(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            toolchains:
              enabled: false
              tools_root: /custom/tools
              install_timeout_seconds: 999
              lock_timeout_seconds: 888
              nodejs:
                default_version: "18"
                mirror: https://internal-mirror.example/dist
                max_old_space_percent: 30
                max_old_space_mb: 512
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        monkeypatch.delenv("TOOLCHAINS_ENABLED", raising=False)
        s = _SettingsIsolated(**self._BASE_KWARGS)
        assert s.toolchains_enabled is False
        assert str(s.toolchains_tools_root) == "/custom/tools"
        assert s.toolchains_install_timeout_seconds == 999
        assert s.toolchains_lock_timeout_seconds == 888
        assert s.toolchains_nodejs_default_version == "18"
        assert s.toolchains_nodejs_mirror == "https://internal-mirror.example/dist"
        assert s.toolchains_nodejs_max_old_space_percent == 30
        assert s.toolchains_nodejs_max_old_space_mb == 512

    def test_node_heap_cap_defaults_to_a_share_of_vm_memory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "does-not-exist.yaml"))
        for var in (
            "TOOLCHAINS_NODEJS_MAX_OLD_SPACE_PERCENT",
            "TOOLCHAINS_NODEJS_MAX_OLD_SPACE_MB",
        ):
            monkeypatch.delenv(var, raising=False)
        s = _SettingsIsolated(**self._BASE_KWARGS)
        assert s.toolchains_nodejs_max_old_space_percent == 40
        assert s.toolchains_nodejs_max_old_space_mb == 0

    def test_node_heap_cap_env_overrides_yaml(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            toolchains:
              nodejs:
                max_old_space_percent: 30
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        monkeypatch.setenv("TOOLCHAINS_NODEJS_MAX_OLD_SPACE_PERCENT", "55")
        s = _SettingsIsolated(**self._BASE_KWARGS)
        assert s.toolchains_nodejs_max_old_space_percent == 55

    @pytest.mark.parametrize(
        "name,value",
        [
            ("TOOLCHAINS_NODEJS_MAX_OLD_SPACE_PERCENT", "101"),
            ("TOOLCHAINS_NODEJS_MAX_OLD_SPACE_PERCENT", "-1"),
            ("TOOLCHAINS_NODEJS_MAX_OLD_SPACE_MB", "-5"),
        ],
    )
    def test_node_heap_cap_rejects_out_of_range_values(self, tmp_path, monkeypatch, name, value):
        monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "does-not-exist.yaml"))
        monkeypatch.setenv(name, value)
        with pytest.raises(ValidationError):
            _SettingsIsolated(**self._BASE_KWARGS)

    def test_missing_yaml_file_falls_back_to_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "does-not-exist.yaml"))
        # mini-swe-agent auto-loads its own global .env on import, which may
        # set these in the real process env — force true isolation here.
        monkeypatch.delenv("MAX_ITERATIONS", raising=False)
        monkeypatch.delenv("BASE_BRANCH", raising=False)
        s = _SettingsIsolated(
            **self._BASE_KWARGS,
        )
        assert s.max_iterations == 50
        # RepoRoute.base_branch still defaults to "main" (no repo_routes
        # configured here, so this is checked on the model directly rather
        # than via Settings, whose single-repo shim was removed).
        assert RepoRoute(issues="a/b").base_branch == "main"

    def test_promoted_constants_default_to_prior_hardcoded_values(self):
        s = _SettingsIsolated(
            **self._BASE_KWARGS,
        )
        assert s.pr_slug_max_length == 30
        assert s.pr_label_agent_generated == "agent-generated"
        assert s.git_user_name == "issue-agent[bot]"
        # 0 = full history. The agent's "already fixed" report has to cite
        # the commit that landed the fix, and `git log -S` needs history.
        assert s.git_clone_depth == 0
        # Empty: the provider supplies the noreply address.
        assert s.git_user_email == ""
        assert s.metrics_task_duration_buckets == [1, 5, 10, 30, 60, 120, 300, 600]
        assert s.metrics_iteration_buckets == [1, 5, 10, 20, 50, 100]
        assert s.prompt_category_map["bug"] == "bug_fix.md"
        assert s.events_retention_days == 30
        assert s.db_retention_days == 90
        assert s.audit_retention_days == 90

    def test_runner_template_knobs_default_to_upstream_yaml_values(self):
        s = _SettingsIsolated(
            **self._BASE_KWARGS,
        )
        # The value mini-swe-agent uses in its own config files — its Python
        # constructor caps nothing at all.
        assert s.llm_observation_max_chars == 10000
        # Matches the "80% of budget" threshold prompts/system.md already names.
        assert s.budget_warn_at_percent == 80

    def test_runner_template_knobs_from_yaml_and_env(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            llm:
              observation_max_chars: 2500
            budgets:
              warn_at_percent: 60
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        monkeypatch.delenv("LLM_OBSERVATION_MAX_CHARS", raising=False)
        monkeypatch.delenv("BUDGET_WARN_AT_PERCENT", raising=False)
        s = _SettingsIsolated(
            **self._BASE_KWARGS,
        )
        assert s.llm_observation_max_chars == 2500
        assert s.budget_warn_at_percent == 60

        monkeypatch.setenv("LLM_OBSERVATION_MAX_CHARS", "999")
        s2 = _SettingsIsolated(
            **self._BASE_KWARGS,
        )
        assert s2.llm_observation_max_chars == 999

    def test_git_clone_depth_from_yaml_and_env(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            git:
              clone_depth: 50
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        monkeypatch.delenv("GIT_CLONE_DEPTH", raising=False)
        s = _SettingsIsolated(
            **self._BASE_KWARGS,
        )
        assert s.git_clone_depth == 50

        monkeypatch.setenv("GIT_CLONE_DEPTH", "1")
        s2 = _SettingsIsolated(
            **self._BASE_KWARGS,
        )
        assert s2.git_clone_depth == 1

    def test_retention_audit_days_from_yaml_and_env(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            retention:
              audit_days: 14
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        monkeypatch.delenv("AUDIT_RETENTION_DAYS", raising=False)
        s = _SettingsIsolated(
            **self._BASE_KWARGS,
        )
        assert s.audit_retention_days == 14

        monkeypatch.setenv("AUDIT_RETENTION_DAYS", "7")
        s2 = _SettingsIsolated(
            **self._BASE_KWARGS,
        )
        assert s2.audit_retention_days == 7

    def test_project_block_absent_yields_empty_defaults(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            providers:
              github:
                type: github
                app_id: 1
            repos:
              - issues: owner/repo
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        monkeypatch.delenv("TEST_COMMAND", raising=False)
        monkeypatch.delenv("LINT_COMMAND", raising=False)
        monkeypatch.delenv("PROTECTED_PATHS", raising=False)
        monkeypatch.delenv("RELEVANT_EXTENSIONS", raising=False)
        s = _SettingsIsolated(**self._BASE_KWARGS)
        project = s.repo_routes[0].project
        assert project.test_command == ""
        assert project.lint_command == ""
        assert project.protected_paths == []
        assert project.relevant_extensions == []

    def test_project_block_partially_filled(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            providers:
              github:
                type: github
                app_id: 1
            repos:
              - issues: owner/repo
                project:
                  test_command: pytest
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        monkeypatch.delenv("LINT_COMMAND", raising=False)
        monkeypatch.delenv("PROTECTED_PATHS", raising=False)
        monkeypatch.delenv("RELEVANT_EXTENSIONS", raising=False)
        s = _SettingsIsolated(**self._BASE_KWARGS)
        project = s.repo_routes[0].project
        assert project.test_command == "pytest"
        assert project.lint_command == ""
        assert project.protected_paths == []
        assert project.relevant_extensions == []

    def test_project_block_fully_filled(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            providers:
              github:
                type: github
                app_id: 1
            repos:
              - issues: owner/repo
                project:
                  test_command: pytest
                  lint_command: ruff check
                  protected_paths: [migrations/, src/engine/]
                  relevant_extensions: [.py]
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        s = _SettingsIsolated(**self._BASE_KWARGS)
        project = s.repo_routes[0].project
        assert project.test_command == "pytest"
        assert project.lint_command == "ruff check"
        assert project.protected_paths == ["migrations/", "src/engine/"]
        assert project.relevant_extensions == [".py"]


class TestRepoRoutes:
    """Settings.repo_routes / route_for / allowed_repos."""

    _BASE_KWARGS = dict(PROVIDERS=github_providers(), LLM_API_KEY="k")

    def _write_yaml(self, tmp_path: Path, contents: str) -> Path:
        p = tmp_path / "config.yaml"
        p.write_text(contents)
        return p

    def test_multiple_repos_each_preserve_their_own_fields(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            repos:
              - issues: owner/a
                target: owner/a-code
                base_branch: dev
                project:
                  test_command: pytest
              - issues: owner/b
                base_branch: trunk
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        s = _SettingsIsolated(**self._BASE_KWARGS)

        assert len(s.repo_routes) == 2

        route_a = s.repo_routes[0]
        assert route_a.issues == "owner/a"
        assert route_a.target_repo == "owner/a-code"
        assert route_a.base_branch == "dev"
        assert route_a.project.test_command == "pytest"

        route_b = s.repo_routes[1]
        assert route_b.issues == "owner/b"
        assert route_b.target_repo == "owner/b"  # target omitted -> defaults to issues
        assert route_b.base_branch == "trunk"
        assert route_b.project.test_command == ""

    def test_route_for_resolves_by_issue_repo(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            repos:
              - issues: owner/x
                target: owner/x-code
              - issues: owner/y
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        s = _SettingsIsolated(**self._BASE_KWARGS)

        found = s.route_for("github", "owner/x")
        assert found is not None
        assert found.target_repo == "owner/x-code"

        assert s.route_for("github", "owner/y") is not None
        assert s.route_for("github", "owner/nao-existe") is None
        assert s.route_for("ghe", "owner/x") is None

    def test_allowed_repos_reflects_all_issue_repos(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            repos:
              - issues: owner/a
              - issues: owner/b
              - issues: owner/c
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        s = _SettingsIsolated(**self._BASE_KWARGS)

        assert s.allowed_repos == ["owner/a", "owner/b", "owner/c"]

    def test_workflow_defaults_to_trunk_based(self):
        route = RepoRoute(issues="owner/repo")
        assert route.workflow == "trunk_based"

    def test_workflow_accepts_gitflow(self):
        route = RepoRoute(issues="owner/repo", workflow="gitflow")
        assert route.workflow == "gitflow"

    def test_workflow_rejects_invalid_value(self):
        with pytest.raises(ValidationError):
            RepoRoute(issues="owner/repo", workflow="rebase_flow")

    def test_invalid_workflow_in_yaml_fails_settings_construction(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            repos:
              - issues: owner/repo
                workflow: rebase_flow
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        with pytest.raises(ValidationError):
            _SettingsIsolated(**self._BASE_KWARGS)


class TestRepoLlmOverride:
    """Per-repo `llm:` override and Settings.llm_profile() — fallback to the
    global `llm:` block is field-by-field, based on which keys are present in
    the repo's YAML, not on the resolved value being None."""

    _BASE_KWARGS = dict(PROVIDERS=github_providers(), LLM_API_KEY="global-key")

    def _write_yaml(self, tmp_path: Path, contents: str) -> Path:
        p = tmp_path / "config.yaml"
        p.write_text(contents)
        return p

    def test_repo_without_llm_block_inherits_every_global_field(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            llm:
              model: global/model
              temperature: 0.3
              fallback:
                model: global/fallback
            repos:
              - issues: owner/repo
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        s = _SettingsIsolated(**self._BASE_KWARGS)
        route = s.route_for("github", "owner/repo")

        assert route.llm is None
        assert s.llm_profile(route) == s.llm_profile(None)

    def test_per_field_override_leaves_the_rest_global(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            llm:
              model: global/model
              max_retries: 5
            repos:
              - issues: owner/repo
                llm:
                  model: repo/model
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        s = _SettingsIsolated(**self._BASE_KWARGS)
        profile = s.llm_profile(s.route_for("github", "owner/repo"))

        assert profile.model == "repo/model"
        assert profile.max_retries == 5  # untouched -> still global

    def test_request_timeout_is_read_from_the_llm_block(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            llm:
              request_timeout_seconds: 30
            repos:
              - issues: owner/repo
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        s = _SettingsIsolated(**self._BASE_KWARGS)

        assert s.llm_request_timeout_seconds == 30

    def test_explicit_null_temperature_resets_a_global_temperature(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            llm:
              temperature: 0.2
            repos:
              - issues: owner/repo
                llm:
                  temperature:
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        s = _SettingsIsolated(**self._BASE_KWARGS)

        assert s.llm_profile(None).temperature == 0.2
        assert s.llm_profile(s.route_for("github", "owner/repo")).temperature is None

    def test_repo_reasoning_effort_override_wins_over_global(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            llm:
              reasoning_effort: low
            repos:
              - issues: owner/repo
                llm:
                  reasoning_effort: high
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        s = _SettingsIsolated(**self._BASE_KWARGS)

        assert s.llm_profile(None).reasoning_effort == "low"
        assert s.llm_profile(s.route_for("github", "owner/repo")).reasoning_effort == "high"

    def test_explicit_null_reasoning_effort_resets_a_global_reasoning_effort(
        self, tmp_path, monkeypatch
    ):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            llm:
              reasoning_effort: high
            repos:
              - issues: owner/repo
                llm:
                  reasoning_effort:
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        s = _SettingsIsolated(**self._BASE_KWARGS)

        assert s.llm_profile(None).reasoning_effort == "high"
        assert s.llm_profile(s.route_for("github", "owner/repo")).reasoning_effort is None

    def test_repo_reasoning_effort_override_rejects_unknown_value(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            repos:
              - issues: owner/repo
                llm:
                  reasoning_effort: ultra
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        with pytest.raises(ValidationError):
            _SettingsIsolated(**self._BASE_KWARGS)

    def test_empty_string_base_url_clears_the_global_base_url(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            llm:
              base_url: https://global.example
            repos:
              - issues: owner/repo
                llm:
                  base_url: ""
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        s = _SettingsIsolated(**self._BASE_KWARGS)

        assert s.llm_profile(None).base_url == "https://global.example"
        assert s.llm_profile(s.route_for("github", "owner/repo")).base_url == ""

    def test_nested_fallback_merges_field_by_field(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            llm:
              fallback:
                model: global/fallback
                base_url: https://global-fallback.example
            repos:
              - issues: owner/repo
                llm:
                  fallback:
                    base_url: https://repo-fallback.example
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        s = _SettingsIsolated(**self._BASE_KWARGS)
        profile = s.llm_profile(s.route_for("github", "owner/repo"))

        assert profile.fallback_model == "global/fallback"  # untouched -> global
        assert profile.fallback_base_url == "https://repo-fallback.example"

    def test_fallback_null_disables_failover_for_this_repo(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            llm:
              fallback:
                model: global/fallback
                base_url: https://global-fallback.example
            repos:
              - issues: owner/repo
                llm:
                  fallback:
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        s = _SettingsIsolated(**self._BASE_KWARGS)
        profile = s.llm_profile(s.route_for("github", "owner/repo"))

        assert profile.fallback_model == ""
        assert profile.fallback_base_url == ""

    def test_api_key_in_repo_llm_block_is_rejected(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            repos:
              - issues: owner/repo
                llm:
                  api_key: sk-leaked
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        with pytest.raises(ValidationError, match="api_key"):
            _SettingsIsolated(**self._BASE_KWARGS)

    def test_unknown_key_in_repo_llm_block_is_rejected(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            repos:
              - issues: owner/repo
                llm:
                  moddel: typo
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        with pytest.raises(ValidationError, match="moddel"):
            _SettingsIsolated(**self._BASE_KWARGS)

    def test_explicit_null_cost_tracking_is_rejected(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            repos:
              - issues: owner/repo
                llm:
                  cost_tracking:
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        with pytest.raises(ValidationError):
            _SettingsIsolated(**self._BASE_KWARGS)

    def test_explicit_null_observation_max_chars_is_rejected(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            repos:
              - issues: owner/repo
                llm:
                  observation_max_chars:
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        with pytest.raises(ValidationError):
            _SettingsIsolated(**self._BASE_KWARGS)

    def test_global_api_key_always_lands_in_the_profile(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            repos:
              - issues: owner/repo
                llm:
                  model: repo/model
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        s = _SettingsIsolated(**self._BASE_KWARGS)
        profile = s.llm_profile(s.route_for("github", "owner/repo"))

        assert profile.api_key == "global-key"

    def test_two_repos_keep_independent_profiles(self, tmp_path, monkeypatch):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            llm:
              model: global/model
            repos:
              - issues: owner/a
                llm:
                  model: repo-a/model
              - issues: owner/b
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        # mini-swe-agent auto-loads its own global .env on import, which may
        # set this in the real process env — force true isolation here.
        monkeypatch.delenv("LLM_MODEL", raising=False)
        s = _SettingsIsolated(**self._BASE_KWARGS)

        assert s.llm_profile(s.route_for("github", "owner/a")).model == "repo-a/model"
        assert s.llm_profile(s.route_for("github", "owner/b")).model == "global/model"

    def test_env_llm_model_wins_over_yaml_global_but_repo_override_wins_overall(
        self, tmp_path, monkeypatch
    ):
        yaml_path = self._write_yaml(
            tmp_path,
            """
            llm:
              model: yaml/global-model
            repos:
              - issues: owner/a
                llm:
                  model: repo/model
              - issues: owner/b
            """,
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        monkeypatch.setenv("LLM_MODEL", "env/global-model")
        s = _SettingsIsolated(**self._BASE_KWARGS)

        # env beats yaml for the global default...
        assert s.llm_profile(s.route_for("github", "owner/b")).model == "env/global-model"
        # ...but a repo-level override still wins over that global, env or not.
        assert s.llm_profile(s.route_for("github", "owner/a")).model == "repo/model"


class TestPolicyCategoriesConfig:
    """policy.triage_only_categories / policy.diagnose_categories — which
    prompt categories route to TRIAGE_ONLY vs. DIAGNOSE (label_router.py's
    classify()). Defaults must reproduce the previously-hardcoded set."""

    def _settings(self, **overrides) -> Settings:
        base = dict(
            PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
            LLM_API_KEY="k",
        )
        base.update(overrides)
        return SettingsDotenv(**base)

    def test_defaults_match_prior_hardcoded_set(self):
        s = self._settings()
        assert set(s.policy_triage_only_categories) == {
            "question",
            "feature_request",
            "high_severity_bug",
        }
        assert s.policy_diagnose_categories == []

    def test_env_overrides(self):
        s = self._settings(
            POLICY_TRIAGE_ONLY_CATEGORIES=["question"],
            POLICY_DIAGNOSE_CATEGORIES=["bug"],
        )
        assert s.policy_triage_only_categories == ["question"]
        assert s.policy_diagnose_categories == ["bug"]

    def test_rejects_unknown_category(self):
        with pytest.raises(ValidationError):
            self._settings(POLICY_TRIAGE_ONLY_CATEGORIES=["not-a-real-category"])

    def test_rejects_overlap_between_the_two_lists(self):
        with pytest.raises(ValidationError):
            self._settings(
                POLICY_TRIAGE_ONLY_CATEGORIES=["bug"],
                POLICY_DIAGNOSE_CATEGORIES=["bug"],
            )

    def test_policy_block_flattened_from_yaml(self):
        from issue_agent.config import flatten_yaml_config

        flat = flatten_yaml_config(
            {
                "policy": {
                    "triage_only_categories": ["question"],
                    "diagnose_categories": ["bug"],
                }
            }
        )
        assert flat["POLICY_TRIAGE_ONLY_CATEGORIES"] == ["question"]
        assert flat["POLICY_DIAGNOSE_CATEGORIES"] == ["bug"]

    def test_policy_absent_yields_no_keys(self):
        from issue_agent.config import flatten_yaml_config

        assert flatten_yaml_config({}) == {}

    def test_yaml_supplies_env_still_wins(self, tmp_path, monkeypatch):
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text("policy:\n  diagnose_categories: [chore]\n")
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        monkeypatch.setenv("POLICY_DIAGNOSE_CATEGORIES", '["docs"]')
        s = _SettingsIsolated(
            PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
            LLM_API_KEY="k",
        )
        assert s.policy_diagnose_categories == ["docs"]  # env wins


class TestConfigFileResolution:
    def test_default_config_file_is_absolute_and_package_relative(self):
        from issue_agent.config import _AGENT_ROOT_DIR, _DEFAULT_CONFIG_FILE

        assert _DEFAULT_CONFIG_FILE.is_absolute()
        assert _DEFAULT_CONFIG_FILE.name == "config.yaml"
        assert _DEFAULT_CONFIG_FILE.parent == _AGENT_ROOT_DIR

    def test_default_env_file_is_absolute(self):
        from issue_agent.config import _DEFAULT_ENV_FILE

        assert _DEFAULT_ENV_FILE.is_absolute()
        assert _DEFAULT_ENV_FILE.name == ".env"


class TestShippedExampleConfig:
    """The committed config.yaml.example is the template operators copy and the
    payload Fly mounts from CONFIG_YAML — it must parse into a valid Settings."""

    _EXAMPLE = Path(__file__).resolve().parents[2] / "config.yaml.example"

    def test_example_exists(self):
        assert self._EXAMPLE.is_file()

    def test_example_parses_into_settings(self, monkeypatch):
        monkeypatch.setenv("CONFIG_FILE", str(self._EXAMPLE))
        for var in ("LLM_MODEL", "LLM_PROVIDER", "MAX_ITERATIONS"):
            monkeypatch.delenv(var, raising=False)
        # Secrets as the env would add them, deep-merged into the YAML instance.
        monkeypatch.setenv("PROVIDERS__GITHUB__WEBHOOK_SECRET", "s")
        monkeypatch.setenv("PROVIDERS__GITHUB__APP_PRIVATE_KEY", "k")
        s = _SettingsIsolated(LLM_API_KEY="k")
        assert len(s.repo_routes) >= 1
        assert s.providers["github"].webhook_secret == "s"
        assert all(r.provider in s.providers for r in s.repo_routes)
        assert s.llm_cost_tracking in ("strict", "lenient")
        # The example's toolchains: block must actually reach Settings, not
        # just parse without error.
        assert s.toolchains_enabled is True
        assert str(s.toolchains_tools_root) == "/data/tools"
        assert s.toolchains_nodejs_default_version == "20"
        assert s.toolchains_nodejs_mirror == "https://nodejs.org/dist"
        assert s.toolchains_nodejs_max_old_space_percent == 40
        assert s.toolchains_nodejs_max_old_space_mb == 0
        assert s.toolchains_install_timeout_seconds == 300
        assert s.toolchains_lock_timeout_seconds == 600
        assert s.prompt_category_map


class TestProviderInstances:
    """`providers.<id>` (PRD §3.5): typed per-instance config, routes bound
    to an instance, secrets only from env, and the old sections refused."""

    def _make(self, **overrides) -> Settings:
        return _SettingsIsolated(LLM_API_KEY="k", **overrides)

    def test_single_instance_is_the_default_route_provider(self):
        s = self._make(PROVIDERS=github_providers(), REPO_ROUTES=[{"issues": "o/r"}])
        assert s.repo_routes[0].provider == "github"
        assert s.route_for("github", "o/r") is s.repo_routes[0]

    def test_several_instances_require_an_explicit_provider(self):
        with pytest.raises(ValidationError, match="needs `provider:`"):
            self._make(
                PROVIDERS={**github_providers(), **github_providers("ghe")},
                REPO_ROUTES=[{"issues": "o/r"}],
            )

    def test_routes_without_any_instance_are_rejected(self):
        with pytest.raises(ValidationError, match="needs `provider:`"):
            self._make(REPO_ROUTES=[{"issues": "o/r"}])

    def test_same_repo_on_two_instances_resolves_per_instance(self):
        s = self._make(
            PROVIDERS={**github_providers(), **github_providers("ghe")},
            REPO_ROUTES=[
                {"issues": "o/r", "provider": "github", "target": "o/public"},
                {"issues": "o/r", "provider": "ghe", "target": "o/internal"},
            ],
        )
        assert s.route_for("github", "o/r").target_repo == "o/public"
        assert s.route_for("ghe", "o/r").target_repo == "o/internal"

    def test_unknown_route_provider_is_rejected(self):
        with pytest.raises(ValidationError, match="'gitlab', which is not configured"):
            self._make(
                PROVIDERS=github_providers(),
                REPO_ROUTES=[{"issues": "o/r", "provider": "gitlab"}],
            )

    def test_duplicate_route_on_one_instance_is_rejected(self):
        with pytest.raises(ValidationError, match="twice"):
            self._make(
                PROVIDERS=github_providers(),
                REPO_ROUTES=[{"issues": "o/r"}, {"issues": "o/r"}],
            )

    @pytest.mark.parametrize("instance_id", ["GHE", "my__ghe", "-ghe", "ghe.corp", "a b"])
    def test_instance_id_must_be_path_and_env_safe(self, instance_id):
        with pytest.raises(ValidationError, match="instance id"):
            self._make(PROVIDERS=github_providers(instance_id))

    def test_unknown_type_is_rejected(self):
        with pytest.raises(ValidationError):
            self._make(PROVIDERS={"x": {"type": "gitlab"}})

    def test_unknown_instance_field_is_rejected(self):
        with pytest.raises(ValidationError):
            self._make(PROVIDERS=github_providers(app_idd=1))

    def test_yaml_instance_with_env_secrets(self, tmp_path, monkeypatch):
        config = tmp_path / "config.yaml"
        config.write_text(
            """
            providers:
              github:
                type: github
                app_id: 1
              ghe:
                type: github
                app_id: 2
                host: https://ghe.example.com
            repos:
              - issues: o/r
                provider: ghe
            """
        )
        monkeypatch.setenv("CONFIG_FILE", str(config))
        monkeypatch.setenv("PROVIDERS__GHE__WEBHOOK_SECRET", "ghe-secret")
        s = self._make()
        assert s.providers["ghe"].webhook_secret == "ghe-secret"
        assert s.providers["ghe"].api_url == "https://ghe.example.com/api/v3"
        assert s.providers["github"].webhook_secret == ""
        assert s.route_for("ghe", "o/r") is not None


class TestProviderYamlRules:
    def test_secret_in_yaml_is_rejected(self):
        from issue_agent.config import flatten_yaml_config

        with pytest.raises(ValueError, match="PROVIDERS__GITHUB__WEBHOOK_SECRET"):
            flatten_yaml_config(
                {"providers": {"github": {"type": "github", "webhook_secret": "x"}}}
            )

    def test_private_key_path_in_yaml_is_rejected(self):
        from issue_agent.config import flatten_yaml_config

        with pytest.raises(ValueError, match="PROVIDERS__GHE__APP_PRIVATE_KEY_PATH"):
            flatten_yaml_config(
                {"providers": {"ghe": {"type": "github", "app_private_key_path": "k.pem"}}}
            )

    @pytest.mark.parametrize("section", ["github", "github_app"])
    def test_legacy_sections_are_rejected(self, section):
        from issue_agent.config import flatten_yaml_config

        with pytest.raises(ValueError, match="migration guide"):
            flatten_yaml_config({section: {"app_id": 1}})

    def test_agent_name_flattens_to_its_own_alias(self):
        from issue_agent.config import flatten_yaml_config

        assert flatten_yaml_config({"agent": {"name": "Bot"}}) == {"AGENT_NAME": "Bot"}

    def test_providers_must_be_a_mapping(self):
        from issue_agent.config import flatten_yaml_config

        with pytest.raises(ValueError, match="mapping"):
            flatten_yaml_config({"providers": [{"type": "github"}]})


class TestNestedEnvSpike:
    """Spike for PRD §3.5: per-instance provider secrets come from env as
    ``PROVIDERS__<ID>__<FIELD>`` and must deep-merge with the instance's
    non-secret fields from config.yaml. Exercised on a minimal settings
    class with the same source order as ``Settings`` (init > env > dotenv >
    YAML), so it pins pydantic-settings' behaviour before ``Settings``
    depends on it."""

    class _GitHub(BaseModel):
        type: Literal["github"]
        app_id: int
        host: str = "https://github.com"
        webhook_secret: str = ""

    class _Spike(BaseSettings):
        model_config = SettingsConfigDict(
            env_file=None, env_prefix="", extra="ignore", env_nested_delimiter="__"
        )
        llm_api_key: str = Field(default="", validation_alias="LLM_API_KEY")
        max_task_retries: int = Field(default=3, validation_alias="MAX_TASK_RETRIES")
        providers: dict[str, Any] = Field(default_factory=dict, validation_alias="PROVIDERS")

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        ):
            yaml = InitSettingsSource(settings_cls, cls._yaml)
            return (init_settings, env_settings, dotenv_settings, yaml, file_secret_settings)

    def _spike(self, yaml: dict[str, Any]) -> type[BaseSettings]:
        provider = Annotated[Union[self._GitHub], Field(discriminator="type")]  # noqa: UP007

        class Spike(self._Spike):
            _yaml: ClassVar[dict[str, Any]] = yaml
            providers: dict[str, provider] = Field(
                default_factory=dict, validation_alias="PROVIDERS"
            )

        return Spike

    def test_env_secret_deep_merges_with_yaml_instance(self, monkeypatch):
        monkeypatch.setenv("PROVIDERS__GITHUB__WEBHOOK_SECRET", "from-env")
        spike = self._spike(
            {"PROVIDERS": {"github": {"type": "github", "app_id": 1, "host": "https://yaml"}}}
        )()
        github = spike.providers["github"]
        assert github.app_id == 1
        assert github.host == "https://yaml"
        assert github.webhook_secret == "from-env"

    def test_env_overrides_yaml_field(self, monkeypatch):
        monkeypatch.setenv("PROVIDERS__GITHUB__HOST", "https://env")
        spike = self._spike(
            {"PROVIDERS": {"github": {"type": "github", "app_id": 1, "host": "https://yaml"}}}
        )()
        assert spike.providers["github"].host == "https://env"

    def test_uppercase_env_id_maps_to_lowercase_instance(self, monkeypatch):
        monkeypatch.setenv("PROVIDERS__GHE__WEBHOOK_SECRET", "ghe-secret")
        spike = self._spike(
            {
                "PROVIDERS": {
                    "github": {"type": "github", "app_id": 1},
                    "ghe": {"type": "github", "app_id": 2},
                }
            }
        )()
        assert set(spike.providers) == {"github", "ghe"}
        assert spike.providers["ghe"].webhook_secret == "ghe-secret"
        assert spike.providers["github"].webhook_secret == ""

    def test_flat_aliases_still_resolve(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "k")
        spike = self._spike({"MAX_TASK_RETRIES": 7})()
        assert spike.llm_api_key == "k"
        assert spike.max_task_retries == 7

    def test_single_member_discriminated_union_rejects_unknown_type(self):
        with pytest.raises(ValidationError):
            self._spike({"PROVIDERS": {"x": {"type": "gitlab", "app_id": 1}}})()

    def test_env_only_instance_without_type_is_rejected(self, monkeypatch):
        monkeypatch.setenv("PROVIDERS__GITHUB__WEBHOOK_SECRET", "s")
        with pytest.raises(ValidationError):
            self._spike({})()


class TestTaskRetentionConfig:
    """retention.tasks.*: days (>= 0, 0 = next maintenance) or "forever"."""

    _BASE_KWARGS = dict(PROVIDERS=github_providers(), LLM_API_KEY="k")

    def test_defaults(self):
        s = _SettingsIsolated(**self._BASE_KWARGS)
        assert s.retention_tasks_pr_merged == 0
        assert s.retention_tasks_pr_closed == 0
        assert s.retention_tasks_aborted == 90
        assert s.retention_tasks_failed == 90
        assert s.retention_tasks_other == 90
        assert s.retention_pr_check_max_per_cycle == 50

    @pytest.mark.parametrize("value", ["forever", "FOREVER", " Forever ", None])
    def test_forever_means_keep_indefinitely(self, value):
        s = _SettingsIsolated(**self._BASE_KWARGS, RETENTION_TASKS_ABORTED=value)
        assert s.retention_tasks_aborted is None

    @pytest.mark.parametrize(("value", "days"), [(0, 0), (30, 30), ("7", 7)])
    def test_days(self, value, days):
        s = _SettingsIsolated(**self._BASE_KWARGS, RETENTION_TASKS_PR_MERGED=value)
        assert s.retention_tasks_pr_merged == days

    @pytest.mark.parametrize("value", [-1, "never-ish", "", 1.5])
    def test_rejects_invalid_values(self, value):
        with pytest.raises(ValidationError):
            _SettingsIsolated(**self._BASE_KWARGS, RETENTION_TASKS_FAILED=value)

    def test_flattened_from_yaml(self):
        from issue_agent.config import flatten_yaml_config

        flat = flatten_yaml_config(
            {
                "retention": {
                    "tasks": {
                        "pr_merged": 3,
                        "pr_closed": "forever",
                        "aborted": None,
                        "failed": 10,
                        "other": 20,
                        "pr_check_max_per_cycle": 5,
                    }
                }
            }
        )
        assert flat["RETENTION_TASKS_PR_MERGED"] == 3
        assert flat["RETENTION_TASKS_PR_CLOSED"] == "forever"
        assert flat["RETENTION_TASKS_ABORTED"] is None
        assert flat["RETENTION_TASKS_FAILED"] == 10
        assert flat["RETENTION_TASKS_OTHER"] == 20
        assert flat["RETENTION_PR_CHECK_MAX_PER_CYCLE"] == 5

    def test_yaml_then_env_precedence(self, tmp_path, monkeypatch):
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(
            "retention:\n  tasks:\n    pr_merged: 30\n    aborted: forever\n    other: null\n"
        )
        monkeypatch.setenv("CONFIG_FILE", str(yaml_path))
        for var in (
            "RETENTION_TASKS_PR_MERGED",
            "RETENTION_TASKS_ABORTED",
            "RETENTION_TASKS_OTHER",
        ):
            monkeypatch.delenv(var, raising=False)
        s = _SettingsIsolated(**self._BASE_KWARGS)
        assert s.retention_tasks_pr_merged == 30
        assert s.retention_tasks_aborted is None
        assert s.retention_tasks_other is None

        monkeypatch.setenv("RETENTION_TASKS_PR_MERGED", "forever")
        monkeypatch.setenv("RETENTION_TASKS_ABORTED", "14")
        s2 = _SettingsIsolated(**self._BASE_KWARGS)
        assert s2.retention_tasks_pr_merged is None
        assert s2.retention_tasks_aborted == 14
