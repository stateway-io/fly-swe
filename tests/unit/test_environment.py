from __future__ import annotations

import inspect
import os
import time
from unittest.mock import patch

import pytest
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import Submitted

from issue_agent.runner.environment import SandboxedLocalEnvironment


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    else:
        return True


_SECRETS = {
    "PROVIDERS__GITHUB__APP_PRIVATE_KEY": "priv-key-sentinel",
    "PROVIDERS__GITHUB__WEBHOOK_SECRET": "webhook-secret-sentinel",
    "LLM_API_KEY": "llm-key-sentinel",
    "ALERT_WEBHOOK_URL": "https://alerts.example.com/sentinel",
    "FLY_API_TOKEN": "fly-token-sentinel",
}


@pytest.fixture
def env(tmp_path) -> SandboxedLocalEnvironment:
    return SandboxedLocalEnvironment(cwd=str(tmp_path), timeout=10)


class TestExecuteStripsSecrets:
    def test_execute_strips_secrets(
        self, env: SandboxedLocalEnvironment, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key, value in _SECRETS.items():
            monkeypatch.setenv(key, value)

        result = env.execute({"command": "env"})

        for key, value in _SECRETS.items():
            assert key not in result["output"]
            assert value not in result["output"]

    def test_execute_keeps_path_and_home(self, env: SandboxedLocalEnvironment) -> None:
        result = env.execute({"command": "printenv PATH; printenv HOME"})
        lines = [line for line in result["output"].splitlines() if line]
        assert len(lines) == 2
        assert lines[0]
        assert lines[1]

    def test_execute_forwards_config_env(self, tmp_path) -> None:
        env = SandboxedLocalEnvironment(cwd=str(tmp_path), timeout=10, env={"FOO_EXTRA": "bar"})
        result = env.execute({"command": "printenv FOO_EXTRA"})
        assert result["output"].strip() == "bar"

    def test_check_finished_still_submits(self, env: SandboxedLocalEnvironment) -> None:
        with pytest.raises(Submitted) as exc_info:
            env.execute({"command": "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\ndone\\n'"})
        assert exc_info.value.messages[0]["extra"]["submission"] == "done\n"

    def test_returncode_and_shape_preserved(self, env: SandboxedLocalEnvironment) -> None:
        result = env.execute({"command": "exit 3"})
        assert result["returncode"] == 3
        assert result["exception_info"] == ""


class TestWarnIfSubmissionMarkerOrphaned:
    """Purely observational: a warning event fires when the submission
    marker is present but not recognized as valid, without changing what
    _check_finished treats as a real submission."""

    @patch("issue_agent.runner.environment.logger")
    def test_no_warning_on_normal_submission(
        self, mock_logger, env: SandboxedLocalEnvironment
    ) -> None:
        with pytest.raises(Submitted):
            env.execute({"command": "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\ndone\\n'"})
        mock_logger.warning.assert_not_called()

    @patch("issue_agent.runner.environment.logger")
    def test_no_warning_when_marker_absent(
        self, mock_logger, env: SandboxedLocalEnvironment
    ) -> None:
        env.execute({"command": "echo hello"})
        mock_logger.warning.assert_not_called()

    @patch("issue_agent.runner.environment.logger")
    def test_warns_when_marker_present_but_not_first_line(
        self, mock_logger, env: SandboxedLocalEnvironment
    ) -> None:
        result = env.execute(
            {
                "command": "echo not-the-first-line; echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT; echo trailer"
            }
        )
        assert result["returncode"] == 0  # not raised as Submitted
        mock_logger.warning.assert_called_once()
        assert (
            mock_logger.warning.call_args.args[0] == "submission_marker_present_but_not_recognized"
        )

    @patch("issue_agent.runner.environment.logger")
    def test_warns_on_backtick_command_substitution_corrupting_the_marker(
        self, mock_logger, env: SandboxedLocalEnvironment
    ) -> None:
        """Reproduces the real production failure: a summary with backticks
        embedded in a double-quoted echo triggers command substitution,
        whose "not found" error lands ahead of the marker line. The
        submission must NOT be recognized (proving _check_finished's real
        behavior is untouched), and the new warning MUST fire (closing the
        observability gap that required pulling trajectory.json over SSH to
        diagnose)."""
        command = 'echo "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n`nonexistent-cmd-xyz`"'

        result = env.execute({"command": command})

        assert result["returncode"] == 0
        mock_logger.warning.assert_called_once()
        assert (
            mock_logger.warning.call_args.args[0] == "submission_marker_present_but_not_recognized"
        )


class TestNonInteractiveEnv:
    """Upstream ships these in its YAML configs; we build the agent in Python,
    so they never reached the agent's shell. A pager pins a command until the
    timeout kills it, and progress bars are paid for as tokens."""

    def test_pager_defaults_are_set(self, env: SandboxedLocalEnvironment) -> None:
        result = env.execute({"command": "printenv PAGER; printenv GIT_PAGER; printenv LESS"})
        assert result["output"].splitlines() == ["cat", "cat", "-R"]

    def test_progress_bars_are_disabled(self, env: SandboxedLocalEnvironment) -> None:
        result = env.execute({"command": "printenv PIP_PROGRESS_BAR; printenv TQDM_DISABLE"})
        assert result["output"].splitlines() == ["off", "1"]

    def test_git_never_prompts_for_credentials(self, env: SandboxedLocalEnvironment) -> None:
        result = env.execute({"command": "printenv GIT_TERMINAL_PROMPT"})
        assert result["output"].strip() == "0"

    def test_config_env_still_wins(self, tmp_path) -> None:
        """config.env is the per-repo project profile's extension point; it
        must be able to override a default, not just add to it."""
        env = SandboxedLocalEnvironment(cwd=str(tmp_path), timeout=10, env={"PAGER": "less"})
        result = env.execute({"command": "printenv PAGER"})
        assert result["output"].strip() == "less"

    def test_defaults_are_literals_not_read_from_the_environment(
        self, env: SandboxedLocalEnvironment, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The env allowlist property still holds: nothing added here copies a
        value out of the worker's environment."""
        monkeypatch.setenv("PAGER", "sekret-pager")
        result = env.execute({"command": "printenv PAGER"})
        assert result["output"].strip() == "cat"


class TestGetTemplateVarsExcludesOsEnviron:
    def test_get_template_vars_excludes_os_environ(
        self, env: SandboxedLocalEnvironment, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_API_KEY", "sekret")
        vars_ = env.get_template_vars()
        assert "LLM_API_KEY" not in vars_
        assert "sekret" not in str(vars_)

    def test_get_template_vars_keeps_config_fields(self, env: SandboxedLocalEnvironment) -> None:
        vars_ = env.get_template_vars()
        assert "cwd" in vars_
        assert "timeout" in vars_


class TestExecuteKillsProcessGroupOnTimeout:
    """A command that outlives its timeout must not leave a live
    grandchild process behind — mirrors the fix mini-swe-agent 2.3.0+ ships
    in LocalEnvironment._run() (start_new_session + os.killpg)."""

    def test_grandchild_process_does_not_survive_timeout(
        self, tmp_path, env: SandboxedLocalEnvironment
    ) -> None:
        pidfile = tmp_path / "child.pid"
        command = f"sleep 30 & echo $! > {pidfile}; wait"

        env.execute({"command": command}, timeout=1)

        pid = int(pidfile.read_text().strip())
        time.sleep(0.5)  # give the SIGKILL a moment to land
        assert not _process_alive(pid), f"grandchild pid {pid} survived the timeout"


class TestCommandsRunAtLowPriority:
    """The worker and web share one vCPU with whatever the agent runs;
    ``nice`` keeps them responsive while a toolchain is busy."""

    def test_agent_commands_run_niced(self, env: SandboxedLocalEnvironment) -> None:
        result = env.execute({"command": "nice"})

        assert result["output"].strip() == "19"

    def test_command_semantics_are_unchanged_by_the_wrapper(
        self, env: SandboxedLocalEnvironment
    ) -> None:
        result = env.execute({"command": "echo $((6*7)) | tr 4 9; exit 5"})

        assert result["output"].strip() == "92"
        assert result["returncode"] == 5

    def test_a_missing_nice_binary_falls_back_to_a_plain_shell(
        self, env: SandboxedLocalEnvironment, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("issue_agent.runner.environment.shutil.which", lambda _name: None)

        result = env.execute({"command": "echo plain"})

        assert result["output"].strip() == "plain"
        assert result["returncode"] == 0

    def test_timeout_still_kills_the_process_group(
        self, tmp_path, env: SandboxedLocalEnvironment
    ) -> None:
        pidfile = tmp_path / "child.pid"

        env.execute({"command": f"sleep 30 & echo $! > {pidfile}; wait"}, timeout=1)

        assert _wait_gone(int(pidfile.read_text().strip()))


class TestExecuteReapsLeftoverProcesses:
    """A command's own process group is only killed on timeout; a process that
    detached itself (``setsid``, ``nohup … &``) used to outlive every command
    and could pile ``tsc`` + ``jest`` onto a 1 GB VM. Whatever is still alive
    once a command returns is now killed, and the agent is told why."""

    def test_setsid_background_process_does_not_survive_the_command(
        self, tmp_path, env: SandboxedLocalEnvironment
    ) -> None:
        pidfile = tmp_path / "bg.pid"
        env.execute({"command": f"setsid sleep 60 >/dev/null 2>&1 & echo $! > {pidfile}"})

        pid = int(pidfile.read_text().strip())
        assert _wait_gone(pid), f"detached pid {pid} survived the command"

    def test_plain_background_process_does_not_survive_the_command(
        self, tmp_path, env: SandboxedLocalEnvironment
    ) -> None:
        pidfile = tmp_path / "bg.pid"
        env.execute({"command": f"sleep 60 >/dev/null 2>&1 & echo $! > {pidfile}"})

        assert _wait_gone(int(pidfile.read_text().strip()))

    def test_observation_tells_the_agent_what_happened(
        self, env: SandboxedLocalEnvironment
    ) -> None:
        result = env.execute({"command": "setsid sleep 60 >/dev/null 2>&1 &"})

        assert "background process" in result["output"]
        assert "killed" in result["output"]
        assert "foreground" in result["output"]
        assert result["returncode"] == 0

    def test_clean_command_output_is_untouched(self, env: SandboxedLocalEnvironment) -> None:
        result = env.execute({"command": "echo hello"})

        assert result["output"] == "hello\n"

    def test_a_submission_is_not_polluted_by_the_notice(
        self, env: SandboxedLocalEnvironment
    ) -> None:
        """Everything after the marker line becomes the PR description."""
        with pytest.raises(Submitted) as exc_info:
            env.execute(
                {
                    "command": "setsid sleep 60 >/dev/null 2>&1 & "
                    "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\nfixed it\\n'"
                }
            )

        assert exc_info.value.messages[0]["extra"]["submission"] == "fixed it\n"

    def test_processes_leaked_by_a_timed_out_command_are_reaped_too(
        self, tmp_path, env: SandboxedLocalEnvironment
    ) -> None:
        pidfile = tmp_path / "bg.pid"
        env.execute(
            {"command": f"setsid sleep 60 >/dev/null 2>&1 & echo $! > {pidfile}; sleep 30"},
            timeout=1,
        )

        assert _wait_gone(int(pidfile.read_text().strip()))

    def test_one_environment_never_reaps_another_ones_processes(self, tmp_path) -> None:
        import subprocess

        a = SandboxedLocalEnvironment(cwd=str(tmp_path), timeout=10)
        b = SandboxedLocalEnvironment(cwd=str(tmp_path), timeout=10)
        out = subprocess.run(
            ["sh", "-c", "setsid sleep 60 >/dev/null 2>&1 & echo $!"],
            env={**os.environ, "ISSUE_AGENT_SANDBOX": b.marker},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        pid = int(out.strip())

        a.execute({"command": "true"})
        assert not _wait_gone(pid, timeout=0.5), "env `a` reaped a process tagged by `b`"

        assert pid in b.reap()
        assert _wait_gone(pid)

    def test_reap_is_a_noop_when_nothing_was_left_running(self, tmp_path) -> None:
        env = SandboxedLocalEnvironment(cwd=str(tmp_path), timeout=10)
        env.execute({"command": "true"})

        assert env.reap() == []


def _wait_gone(pid: int, timeout: float = 3.0) -> bool:
    """True once ``pid`` is gone — a zombie awaiting its parent counts as gone."""
    from pathlib import Path

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return True
        if state == "Z":
            return True
        time.sleep(0.05)
    return False


def test_upstream_execute_signature_is_pinned() -> None:
    """Drift guard for the Phase 2 bump to mini-swe-agent 2.4.6.

    SandboxedLocalEnvironment.execute is a near-verbatim copy of
    LocalEnvironment.execute (mini-swe-agent 2.2.8). If upstream changes the
    signature, this test fails loudly so the copy gets re-synced.
    """
    assert (
        str(inspect.signature(LocalEnvironment.execute))
        == "(self, action: dict, cwd: str = '', *, timeout: int | None = None) -> dict[str, typing.Any]"
    )
