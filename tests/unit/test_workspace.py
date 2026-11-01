from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.providers.github.auth import GitHubAppAuth
from issue_agent.providers.github.transport import GitHubTransport
from issue_agent.runner.workspace import Workspace
from tests.fakes.fake_provider import FakeProvider
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
        DATA_DIR="/tmp/test-workspace-data",
        REPO_ROUTES=[
            {
                "issues": "stateway-io/stateway-issues",
                "target": "stateway-io/stateway-agent-fixtures",
            }
        ],
    )
    defaults.update(overrides)
    return _SettingsNoEnv(**defaults)


@pytest.fixture
def settings() -> Settings:
    return _make_settings()


@pytest.fixture
def mock_auth() -> MagicMock:
    auth = MagicMock(spec=GitHubAppAuth)
    auth.token_for.return_value = "ghs_test_token_123"
    return auth


_TARGET_REPO = "stateway-io/stateway-agent-fixtures"


def _workspace(task_id: str, settings: Settings, auth: MagicMock) -> Workspace:
    return Workspace(
        task_id=task_id,
        target_repo=_TARGET_REPO,
        settings=settings,
        transport=GitHubTransport(auth, settings.providers["github"].host),
    )


@pytest.fixture
def workspace(settings: Settings, mock_auth: MagicMock) -> Workspace:
    return _workspace("task-abc-123", settings, mock_auth)


class TestWorkspace:
    def test_path_format(self, workspace: Workspace) -> None:
        expected = Path(
            "/tmp/test-workspace-data/workspaces/task-abc-123/stateway-io/stateway-agent-fixtures"
        )
        assert workspace.path == expected

    def test_path_with_custom_data_dir(self, mock_auth: MagicMock) -> None:
        settings = _make_settings(DATA_DIR="/custom/data")
        ws = _workspace("t1", settings, mock_auth)
        assert ws.path == Path("/custom/data/workspaces/t1/stateway-io/stateway-agent-fixtures")

    def test_explicit_git_user_email_wins_over_the_provider_default(
        self, mock_auth: MagicMock
    ) -> None:
        settings = _make_settings(GIT_USER_EMAIL="bot@example.com")
        ws = _workspace("t-email", settings, mock_auth)

        with (
            patch("shutil.rmtree"),
            patch.object(Path, "exists", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            ws.clone(target_branch="main")

        assert mock_run.call_args_list[2][0][0] == [
            "git",
            "config",
            "user.email",
            "bot@example.com",
        ]

    def test_clone_creates_directory_and_runs_git(
        self, workspace: Workspace, settings: Settings
    ) -> None:
        ws_path = workspace.path

        with (
            patch("shutil.rmtree") as mock_rmtree,
            patch.object(Path, "exists", return_value=True) as mock_exists,
            patch("subprocess.run") as mock_run,
        ):
            result = workspace.clone(target_branch="main")

        assert result == ws_path
        mock_exists.assert_called()
        mock_rmtree.assert_called_once_with(ws_path, ignore_errors=True)

        assert mock_run.call_count == 3

        clone_call = mock_run.call_args_list[0]
        assert clone_call[0][0] == [
            "git",
            "clone",
            "--branch",
            "main",
            "https://github.com/stateway-io/stateway-agent-fixtures.git",
            str(ws_path),
        ]
        assert clone_call[1]["check"] is True
        assert clone_call[1]["capture_output"] is True

        clone_env = clone_call[1]["env"]
        assert clone_env["GIT_CONFIG_KEY_0"] == "http.https://github.com.extraHeader"
        expected_cred = base64.b64encode(b"x-access-token:ghs_test_token_123").decode()
        assert clone_env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {expected_cred}"

        user_call = mock_run.call_args_list[1]
        assert user_call[0][0] == ["git", "config", "user.name", settings.git_user_name]
        assert user_call[1]["cwd"] == ws_path

        email_call = mock_run.call_args_list[2]
        # GIT_USER_EMAIL unset: the provider's noreply address for the name.
        assert email_call[0][0] == [
            "git",
            "config",
            "user.email",
            "issue-agent[bot]@users.noreply.github.com",
        ]
        assert email_call[1]["cwd"] == ws_path

    def test_clone_is_full_history_by_default(self, workspace: Workspace) -> None:
        # The agent has to run `git log -S` to cite the commit that already
        # landed a fix before reporting that an issue needs no change
        # (prompts/system.md, WHEN TO STOP). A shallow clone has nothing to
        # search and the agent cannot deepen it itself, so the default
        # carries full history.
        with (
            patch.object(Path, "exists", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            workspace.clone()

        argv = mock_run.call_args_list[0][0][0]
        assert not any(arg.startswith("--depth") for arg in argv), argv

    def test_clone_depth_bounds_history_when_set(self, mock_auth: MagicMock) -> None:
        settings = _make_settings(GIT_CLONE_DEPTH=50)
        ws = _workspace("t1", settings, mock_auth)

        with (
            patch.object(Path, "exists", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            ws.clone()

        argv = mock_run.call_args_list[0][0][0]
        assert argv[:3] == ["git", "clone", "--depth=50"]

    def test_clone_cleans_existing_directory(self, workspace: Workspace) -> None:
        ws_path = workspace.path

        with (
            patch.object(Path, "exists", return_value=True) as mock_exists,
            patch("shutil.rmtree") as mock_rmtree,
            patch("subprocess.run") as mock_run,
        ):
            workspace.clone()

        mock_exists.assert_called()
        mock_rmtree.assert_called_once_with(ws_path, ignore_errors=True)
        assert mock_run.call_count == 3

    def test_clone_uses_custom_branch(self, workspace: Workspace) -> None:
        with (
            patch.object(Path, "exists", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            workspace.clone(target_branch="develop")

        clone_call = mock_run.call_args_list[0]
        assert "--branch" in clone_call[0][0]
        branch_idx = clone_call[0][0].index("--branch") + 1
        assert clone_call[0][0][branch_idx] == "develop"

    def test_clone_propagates_subprocess_error(self, workspace: Workspace) -> None:
        with (
            patch.object(Path, "exists", return_value=False),
            patch("subprocess.run", side_effect=subprocess.CalledProcessError(128, "git clone")),
            pytest.raises(subprocess.CalledProcessError),
        ):
            workspace.clone()

    def test_clone_url_never_contains_a_token(
        self, workspace: Workspace, mock_auth: MagicMock
    ) -> None:
        """A2: the remote URL is always tokenless — git persists whatever URL
        it's given into .git/config, so a tokened URL here would leak the
        credential to anything (including the agent) that can read the
        cloned workspace."""
        with (
            patch.object(Path, "exists", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            workspace.clone()

        cmd_list = mock_run.call_args_list[0][0][0]
        clone_url = cmd_list[-2]
        assert clone_url == "https://github.com/stateway-io/stateway-agent-fixtures.git"
        assert "ghs_test_token_123" not in clone_url

    def test_clone_authenticates_via_env_not_url(
        self, workspace: Workspace, mock_auth: MagicMock
    ) -> None:
        with (
            patch.object(Path, "exists", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            workspace.clone()

        clone_call = mock_run.call_args_list[0]
        clone_env = clone_call[1]["env"]
        expected_cred = base64.b64encode(b"x-access-token:ghs_test_token_123").decode()
        assert clone_env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {expected_cred}"
        # The credential is the one for the repo being cloned.
        mock_auth.token_for.assert_called_once_with(_TARGET_REPO)

    def test_clone_scopes_the_auth_header_to_a_custom_github_host(
        self, mock_auth: MagicMock
    ) -> None:
        ws = _workspace(
            "t-ghe",
            _make_settings(PROVIDERS=github_providers(host="https://ghe.example.com")),
            mock_auth,
        )
        with (
            patch.object(Path, "exists", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            ws.clone()

        clone_call = mock_run.call_args_list[0]
        assert f"https://ghe.example.com/{_TARGET_REPO}.git" in clone_call[0][0]
        assert clone_call[1]["env"]["GIT_CONFIG_KEY_0"] == (
            "http.https://ghe.example.com.extraHeader"
        )

    def test_cleanup_removes_directory(self, workspace: Workspace) -> None:
        with (
            patch.object(Path, "exists", return_value=True) as mock_exists,
            patch("shutil.rmtree") as mock_rmtree,
        ):
            workspace.cleanup()

        mock_exists.assert_called_once()
        mock_rmtree.assert_called_once_with(workspace.path, ignore_errors=True)

    def test_cleanup_skips_if_not_exists(self, workspace: Workspace) -> None:
        with (
            patch.object(Path, "exists", return_value=False) as mock_exists,
            patch("shutil.rmtree") as mock_rmtree,
        ):
            workspace.cleanup()

        mock_exists.assert_called_once()
        mock_rmtree.assert_not_called()

    def test_remote_url_reflects_default_github_host(self, mock_auth: MagicMock) -> None:
        """Sanity check for the default: with no providers.github.host
        override, the host stays "https://github.com" and _remote_url uses
        it (not a hardcoded literal — see test below for the custom case)."""
        settings = _make_settings()
        ws = _workspace("t-default-host", settings, mock_auth)
        assert settings.providers["github"].host == "https://github.com"
        assert ws._remote_url == f"https://github.com/{_TARGET_REPO}.git"

    def test_remote_url_reflects_custom_github_host_from_yaml(
        self, mock_auth: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end regression guard for providers.github.host (YAML) ->
        the instance's settings -> Workspace._remote_url. Every other clone/
        _remote_url test in this file uses the default host, so they'd all
        still pass even if _remote_url regressed to the old hardcoded
        "https://github.com" literal — this is the one test that would catch
        that regression.
        """
        custom_host = "https://ghe.example.internal"
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            f"""
            providers:
              github:
                type: github
                app_id: 1
                host: {custom_host}
            repos:
              - issues: stateway-io/stateway-issues
                target: {_TARGET_REPO}
            """
        )
        monkeypatch.setenv("CONFIG_FILE", str(config_path))
        monkeypatch.delenv("PROVIDERS__GITHUB__HOST", raising=False)
        # Secrets come from env and deep-merge into the YAML instance.
        monkeypatch.setenv("PROVIDERS__GITHUB__APP_PRIVATE_KEY", "fake-key")
        monkeypatch.setenv("PROVIDERS__GITHUB__WEBHOOK_SECRET", "s")

        class _SettingsFromYaml(Settings):
            model_config = SettingsConfigDict(
                env_file=None,
                env_file_encoding="utf-8",
                env_prefix="",
                extra="ignore",
            )

        settings = _SettingsFromYaml(
            LLM_API_KEY="k",
            DATA_DIR="/tmp/test-workspace-data",
        )
        assert settings.providers["github"].host == custom_host

        ws = _workspace("t-custom-host", settings, mock_auth)
        assert ws._remote_url == f"{custom_host}/{_TARGET_REPO}.git"


class TestWorkspaceSlugAndTransport:
    """The workspace treats the repo slug as opaque and gets every URL and
    credential from the forge's transport (PRD §3.2)."""

    def test_clone_url_and_credential_come_from_the_transport(self, settings: Settings) -> None:
        plugin = FakeProvider("fake", secret="s")
        ws = Workspace("t1", "group/sub/project", settings, plugin.transport())

        with (
            patch.object(Path, "exists", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            ws.clone()

        clone_call = mock_run.call_args_list[0]
        assert clone_call[0][0][-2] == "https://forge.example/group/sub/project.git"
        assert plugin.forge.tokens == ["fake-token-1"]
        assert "fake-token-1" not in " ".join(clone_call[0][0])

    def test_a_nested_slug_nests_the_path(self, settings: Settings) -> None:
        ws = Workspace("t1", "group/sub/project", settings, MagicMock())
        assert ws.path == Path(settings.data_dir, "workspaces", "t1", "group", "sub", "project")

    @pytest.mark.parametrize("slug", ["../escape", "owner/../../etc", "/abs/repo", ""])
    def test_a_slug_that_escapes_the_task_dir_is_rejected(
        self, settings: Settings, slug: str
    ) -> None:
        with pytest.raises(ValueError, match="invalid repo slug"):
            Workspace("t1", slug, settings, MagicMock())
