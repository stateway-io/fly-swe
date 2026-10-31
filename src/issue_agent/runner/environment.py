from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import uuid
from typing import Any

from minisweagent.environments.local import LocalEnvironment
from minisweagent.utils.serialize import recursive_merge

from issue_agent.logging import get_logger
from issue_agent.runner.guards import SANDBOX_MARKER_ENV, reap_tagged_processes

logger = get_logger(__name__)

# Allowlist for the agent's bash subprocess environment (finding A2).
#
# `LocalEnvironment.execute` (mini-swe-agent 2.2.8) runs every agent command
# with `env=os.environ | self.config.env` — `config.env` can only ADD keys,
# never remove them, so the worker process's full environment (including the
# five secrets below) is otherwise handed to the LLM-driven shell verbatim.
#
# This is an allowlist, not a denylist: `FLY_API_TOKEN` isn't even a
# `Settings` field, which proves a list of "known secrets" is incomplete by
# construction. Only what a bash agent running git/python/node genuinely
# needs is forwarded.
_ENV_ALLOWLIST = (
    "PATH",  # locate git, python, node, sh, coreutils
    "HOME",  # git reads ~/.gitconfig; pip/npm/uv caches; python user site
    "USER",
    "LOGNAME",  # tools calling getpwuid()/whoami; some build/test scripts
    "SHELL",  # subprocess(shell=True) uses /bin/sh; tools inspect $SHELL
    "TERM",  # git/less avoid "unknown terminal" warnings
    "TZ",  # correct local timestamps
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",  # UTF-8 output from python/click/git; click aborts without a usable locale
    "TMPDIR",
    "PWD",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",  # TLS trust store, if the base image sets these explicitly
)
# Everything the harness runs (agent commands, dependency installs, the test
# command) shares one shared vCPU with the worker and the web server; at the
# lowest priority they stay responsive while tsc/jest/npm are busy.
_NICE_LEVEL = 19
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

_SUBMISSION_MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"

# Non-interactive defaults from upstream's own config files
# (``minisweagent/config/default.yaml``, ``mini.yaml``, ``benchmarks/swebench.yaml``),
# plus the two git ones our flow needs. We build the agent in Python rather
# than from those YAMLs, so none of this reached the agent's shell before.
#
# Why it matters: a command that opens a pager (``git log``, ``man``) blocks
# until ``command_timeout_seconds`` kills it, and progress bars (pip, tqdm)
# fill the observation with ``\r`` noise that is paid for as tokens on every
# subsequent step.
#
# These are *additions* to the environment, not ``_ENV_ALLOWLIST`` entries:
# every value is a literal, none is read from ``os.environ``, so they open no
# path for a secret to reach the agent (A2).
_NONINTERACTIVE_ENV = {
    "PAGER": "cat",
    "MANPAGER": "cat",
    "GIT_PAGER": "cat",
    "LESS": "-R",
    "PIP_PROGRESS_BAR": "off",
    "TQDM_DISABLE": "1",
    # The harness sets this for its own git calls (``git_auth.git_auth_env``);
    # this one is for the agent's shell, a different process.
    "GIT_TERMINAL_PROMPT": "0",
    "DEBIAN_FRONTEND": "noninteractive",
    "PYTHONUNBUFFERED": "1",
}


def _warn_if_submission_marker_orphaned(output: dict) -> None:
    """Purely observational: logs when the submission marker is present
    somewhere in a command's output but ``LocalEnvironment._check_finished``
    (called right after this, unmodified) won't recognize it as a valid
    submission — e.g. because shell metacharacters (backticks, ``$vars``) in
    the agent's own summary text triggered command substitution inside a
    double-quoted ``echo``, pushing error output ahead of the marker line
    (see ``prompts/system.md``'s completion contract). Never raises and never
    changes what counts as a valid submission — that stays exactly
    upstream's own first-line/returncode-0 check."""
    text = output.get("output", "")
    if _SUBMISSION_MARKER not in text:
        return
    lines = text.lstrip().splitlines(keepends=True)
    matched = bool(lines) and lines[0].strip() == _SUBMISSION_MARKER and output.get("returncode") == 0
    if not matched:
        logger.warning(
            "submission_marker_present_but_not_recognized",
            returncode=output.get("returncode"),
            output_excerpt=text[:1000],
        )


def _sandboxed_env(extra: dict[str, str]) -> dict[str, str]:
    base = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    base.setdefault("PATH", _DEFAULT_PATH)
    # ``extra`` (``config.env``) last: the per-repo project profile stays free
    # to override any of the non-interactive defaults.
    return base | _NONINTERACTIVE_ENV | dict(extra)


class SandboxedLocalEnvironment(LocalEnvironment):
    """``LocalEnvironment`` that never exposes the worker's secrets to the agent.

    ``execute`` is a near-verbatim copy of
    ``minisweagent.environments.local.LocalEnvironment.execute`` (mini-swe-agent
    2.4.6, see ``.venv/.../minisweagent/environments/local.py``), with a single
    change: ``env=`` is built from an explicit allowlist instead of
    ``os.environ | self.config.env``. ``tests/unit/test_environment.py::
    test_upstream_execute_signature_is_pinned`` fails loudly if the upstream
    signature drifts, which is the signal to re-check this copy.

    Project-specific environment (CI variables, ``NODE_ENV``, test database
    URLs, ...) is deliberately out of scope here — that is the per-repo
    project profile of Phase 4 (``config.env`` remains the extension point).

    Every command runs tagged with a per-instance marker
    (``guards.SANDBOX_MARKER_ENV``), and whatever is still alive once a command
    returns — a ``setsid``/``nohup … &`` process the agent detached to dodge
    the command timeout — is killed (``reap``), with a notice in the
    observation so the agent stops doing it.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.marker = uuid.uuid4().hex

    def reap(self) -> list[int]:
        """Kill every process this environment's commands left running."""
        return reap_tagged_processes(self.marker)

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")
        cwd = cwd or self.config.cwd or os.getcwd()
        try:
            env = _sandboxed_env(self.config.env) | {SANDBOX_MARKER_ENV: self.marker}
            result = _run(command, cwd, env, timeout or self.config.timeout)
            output = {"output": result.stdout, "returncode": result.returncode, "exception_info": ""}
        except Exception as e:
            raw_output = getattr(e, "output", None)
            raw_output = (
                raw_output.decode("utf-8", errors="replace") if isinstance(raw_output, bytes) else (raw_output or "")
            )
            output = {
                "output": raw_output,
                "returncode": -1,
                "exception_info": f"An error occurred while executing the command: {e}",
                "extra": {"exception_type": type(e).__name__, "exception": str(e)},
            }
        self._reap_leftovers(command, output)
        _warn_if_submission_marker_orphaned(output)
        self._check_finished(output)
        return output

    def _reap_leftovers(self, command: str, output: dict) -> None:
        """Kill what the command left running and tell the agent (in place).

        The notice is skipped on a submission: everything after the marker line
        becomes the PR description, and must not carry harness chatter.
        """
        killed = self.reap()
        if not killed:
            return
        logger.warning("agent_background_processes_reaped", pids=killed, command=command[:200])
        text = output.get("output", "")
        if _SUBMISSION_MARKER in text:
            return
        separator = "" if not text or text.endswith("\n") else "\n"
        output["output"] = (
            f"{text}{separator}[harness: {len(killed)} background process(es) started by this "
            "command were killed when it returned. Detaching processes (&, nohup, setsid) is "
            "not allowed — run long commands in the foreground, one at a time.]\n"
        )

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        # Drop os.environ from the Jinja namespace entirely (A1 defense in
        # depth): upstream's get_template_vars merges the full worker
        # environment into every rendered template.
        return recursive_merge(self.config.model_dump(), platform.uname()._asdict(), kwargs)


def _run(command: str, cwd: str, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Like subprocess.run, but kills the whole process group on timeout so no children are orphaned.
    The command runs as ``nice -n 19 /bin/sh -c`` (plain ``/bin/sh -c`` where ``nice`` is missing).

    Verbatim port of ``minisweagent.environments.local._run`` (2.3.0+, see A6):
    upstream's own ``LocalEnvironment.execute`` calls this, but our
    ``execute()`` override above can't call it directly without also
    inheriting ``os.environ | self.config.env`` (the A2 secrets leak), so the
    helper is duplicated here with an explicit ``env`` parameter instead.
    """
    nice = shutil.which("nice")
    # ``nice`` execs the shell, so the pid (and the process group below) is the shell's.
    argv = [nice, "-n", str(_NICE_LEVEL), "/bin/sh", "-c", command] if nice else ["/bin/sh", "-c", command]
    process = subprocess.Popen(
        argv,
        text=True,
        cwd=cwd,
        env=env,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGKILL) if os.name == "posix" else process.kill()
        stdout, _ = process.communicate()
        raise subprocess.TimeoutExpired(command, timeout, output=stdout) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout=stdout)
