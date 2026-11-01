"""Acceptance tests for the baseline security guarantees:

1. An issue body containing Jinja syntax is never rendered/evaluated (A1).
2. The agent's bash environment carries none of the five secrets (A2).
3. The workspace's .git/config never contains a git credential (A2).
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.providers.github.transport import GitHubTransport
from issue_agent.queue.models import Task, TriggerKind
from issue_agent.runner.agent_factory import build_agent
from issue_agent.runner.prompts import build_user_message
from issue_agent.runner.workspace import Workspace
from tests.helpers.factories import make_author, make_issue, make_trigger_event
from tests.helpers.fake_model import FakeModel, _make_bash_response, exit_response
from tests.helpers.github import GITHUB_TERMS as TERMS
from tests.helpers.settings import github_providers

if TYPE_CHECKING:
    from pathlib import Path

_SECRET_ENV_VARS = (
    "PROVIDERS__GITHUB__APP_PRIVATE_KEY",
    "PROVIDERS__GITHUB__WEBHOOK_SECRET",
    "LLM_API_KEY",
    "ALERT_WEBHOOK_URL",
    "FLY_API_TOKEN",
)


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


def _make_settings(**overrides: dict) -> Settings:
    repo_routes = overrides.pop(
        "REPO_ROUTES",
        [
            {
                "issues": "stateway-io/stateway-issues",
                "target": "stateway-io/stateway-agent-fixtures",
            }
        ],
    )
    defaults: dict = dict(
        PROVIDERS=github_providers(app_id=1, app_private_key="k", webhook_secret="s"),
        LLM_API_KEY="k",
        DATA_DIR="/tmp/test-phase1-security-data",
        REPO_ROUTES=repo_routes,
    )
    defaults.update(overrides)
    return SettingsNoEnv(**defaults)


def _make_task(body: str, **overrides: dict) -> Task:
    trigger = make_trigger_event(
        issue=make_issue(
            number=42,
            title="Phase 1 security acceptance",
            body=body,
            labels=["docs"],
        ),
        actor=make_author(login="testuser"),
    )
    defaults: dict = dict(
        delivery_id="delivery-phase1-1",
        repo="stateway-io/stateway-issues",
        issue_number=42,
        trigger_kind=TriggerKind.ISSUE_OPENED,
        trigger_json=trigger.model_dump_json(),
    )
    defaults.update(overrides)
    return Task(**defaults)


def _all_message_text(agent) -> str:
    return "\n".join(str(m.get("content", "")) for m in agent.messages)


@pytest.fixture(autouse=True)
def _cleanup_data_dir() -> None:
    yield
    import shutil

    shutil.rmtree("/tmp/test-phase1-security-data", ignore_errors=True)


class TestIssueBodyTemplateInjectionIsInert:
    """Acceptance 1: '{{ LLM_API_KEY }}' and '{% for' in the issue body — nothing
    is rendered and the task is not brought down."""

    def test_injection_payload_reaches_the_model_as_literal_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_API_KEY", "sk-PHASE1-SECRET")

        malicious_body = (
            "{{ LLM_API_KEY }} {{ 7*7 }} {% for x in y %}{% endfor %} {{ model_kwargs }}"
        )
        task = _make_task(malicious_body)
        settings = _make_settings()
        route = settings.repo_routes[0]

        agent = build_agent(
            task, tmp_path, settings, route, terms=TERMS, model=FakeModel([exit_response()])
        )

        # Must not raise TemplateSyntaxError / UndefinedError, and must not
        # crash the task.
        agent.run(task=build_user_message(task, TERMS))

        system_content = str(agent.messages[0]["content"])
        user_content = str(agent.messages[1]["content"])

        assert agent.messages[1]["role"] == "user"
        assert "{{ 7*7 }}" in user_content
        assert "{% for x in y %}" in user_content
        assert "{{ LLM_API_KEY }}" in user_content
        assert "49" not in user_content
        assert "sk-PHASE1-SECRET" not in user_content
        assert "sk-PHASE1-SECRET" not in system_content


class TestAgentEnvironmentHasNoSecrets:
    """Acceptance 2: running `env` in the agent's environment proves the
    absence of all five secrets."""

    def test_env_command_output_excludes_all_five_secrets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinels = {name: f"sentinel-{name.lower()}" for name in _SECRET_ENV_VARS}
        for name, value in sentinels.items():
            monkeypatch.setenv(name, value)

        task = _make_task("A normal issue body, nothing adversarial.")
        settings = _make_settings()
        route = settings.repo_routes[0]
        fake = FakeModel([_make_bash_response("env"), exit_response()])

        agent = build_agent(task, tmp_path, settings, route, terms=TERMS, model=fake)
        agent.run(task=build_user_message(task, TERMS))

        transcript = _all_message_text(agent)
        for name, value in sentinels.items():
            assert name not in transcript, f"{name} name leaked into agent transcript"
            assert value not in transcript, f"{name} value leaked into agent transcript"
        assert "PATH=" in transcript


class TestWorkspaceGitConfigHasNoCredential:
    """Acceptance 3: `.git/config` in the agent's workspace contains no
    credential.

    Exercises the REAL, unpatched `Workspace._remote_url` (the GitHub
    transport's clone URL, still literally
    ``https://github.com/<owner>/<repo>.git``) and the real `clone()`
    subprocess. Since tests have no network access to github.com, the only
    thing redirected is where git connects — via git's own
    ``url.<base>.insteadOf`` rewrite, pointed at a local bare repo through
    ``GIT_CONFIG_GLOBAL`` — a mechanism entirely separate from
    `git_auth_env()`, which is exercised unmodified. If `_remote_url` ever
    regressed to embedding a token, this test would still clone
    successfully (the token would just be an inert part of the URL) and
    would then fail on the `.git/config` assertions below.
    """

    @pytest.fixture
    def redirected_github(self, tmp_path: Path) -> tuple[str, str, Path]:
        owner, repo = "acme", "widgets"
        remotes_root = tmp_path / "remotes"
        bare = remotes_root / owner / f"{repo}.git"
        bare.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "--bare", "-b", "main"], cwd=bare, check=True, capture_output=True
        )

        seed = tmp_path / "seed"
        seed.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=seed, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "bot@test.com"],
            cwd=seed,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test Bot"], cwd=seed, check=True, capture_output=True
        )
        (seed / "README.md").write_text("seed\n")
        subprocess.run(["git", "add", "-A"], cwd=seed, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "seed"], cwd=seed, check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", str(bare)], cwd=seed, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "push", "-u", "origin", "main"], cwd=seed, check=True, capture_output=True
        )

        gitconfig = tmp_path / "redirect.gitconfig"
        gitconfig.write_text(f'[url "{remotes_root}/"]\n\tinsteadOf = https://github.com/\n')
        return owner, repo, gitconfig

    def test_real_clone_leaves_no_credential_in_git_config(
        self,
        redirected_github: tuple[str, str, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        owner, repo, gitconfig = redirected_github
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))

        test_token = "ghs_phase1_test_token_value"  # noqa: S105 - test fixture value
        settings = _make_settings(
            DATA_DIR=str(tmp_path / "data"),
            REPO_ROUTES=[{"issues": "stateway-io/stateway-issues", "target": f"{owner}/{repo}"}],
        )
        auth = MagicMock()
        auth.token_for.return_value = test_token

        ws = Workspace(
            task_id="t-phase1",
            target_repo=f"{owner}/{repo}",
            settings=settings,
            transport=GitHubTransport(auth, settings.providers["github"].host),
        )
        ws_path = ws.clone(target_branch="main")

        git_config_text = (ws_path / ".git" / "config").read_text()
        assert test_token not in git_config_text
        assert "x-access-token" not in git_config_text
        assert "Authorization" not in git_config_text
        assert "github.com" in git_config_text  # sanity: origin is the real, unpatched URL

    def test_agent_reading_git_config_sees_no_credential(
        self,
        redirected_github: tuple[str, str, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        owner, repo, gitconfig = redirected_github
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))

        test_token = "ghs_phase1_test_token_value"  # noqa: S105 - test fixture value
        settings = _make_settings(
            DATA_DIR=str(tmp_path / "data"),
            REPO_ROUTES=[{"issues": "stateway-io/stateway-issues", "target": f"{owner}/{repo}"}],
        )
        auth = MagicMock()
        auth.token_for.return_value = test_token

        ws = Workspace(
            task_id="t-phase1-agent",
            target_repo=f"{owner}/{repo}",
            settings=settings,
            transport=GitHubTransport(auth, settings.providers["github"].host),
        )
        ws_path = ws.clone(target_branch="main")

        task = _make_task("A normal issue body.")
        fake = FakeModel([_make_bash_response("cat .git/config"), exit_response()])
        route = settings.repo_routes[0]
        agent = build_agent(task, ws_path, settings, route, terms=TERMS, model=fake)
        agent.run(task=build_user_message(task, TERMS))

        transcript = _all_message_text(agent)
        assert test_token not in transcript
        assert "x-access-token" not in transcript
        assert "Authorization" not in transcript
