"""Containment of what the agent leaves running on the (small) Fly VM.

The agent used ``setsid bash -c '…' &`` to dodge the harness' per-command
``killpg`` and left ``tsc`` and ``jest`` running side by side on a 1 GB VM with
no swap, which pushed it into memory thrashing. Anything still alive once a
command returned is therefore reaped, found through an environment marker that
descendants inherit even after ``setsid`` and re-parenting.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from issue_agent.runner.guards import (
    SANDBOX_MARKER_ENV,
    find_tagged_pids,
    reap_tagged_processes,
)

pytestmark = pytest.mark.skipif(
    not Path("/proc/self/environ").exists(), reason="needs a Linux /proc"
)


def _alive(pid: int) -> bool:
    """True while the process runs — a zombie (killed, not yet reaped) is dead."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"


def _wait_dead(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _spawn_detached(script: str, marker: str | None) -> int:
    """Run ``script`` in a shell that prints the pid it leaves running."""
    env = dict(os.environ)
    if marker is not None:
        env[SANDBOX_MARKER_ENV] = marker
    out = subprocess.run(
        ["sh", "-c", script], env=env, capture_output=True, text=True, timeout=10, check=True
    ).stdout
    return int(out.strip().splitlines()[-1])


@pytest.fixture
def marker() -> str:
    return uuid.uuid4().hex


@pytest.fixture(autouse=True)
def _cleanup() -> object:
    pids: list[int] = []
    yield pids
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


class TestFindTaggedPids:
    def test_finds_a_setsid_process_that_outlived_its_shell(self, marker, _cleanup) -> None:
        pid = _spawn_detached("setsid sleep 60 >/dev/null 2>&1 & echo $!", marker)
        _cleanup.append(pid)

        assert pid in find_tagged_pids(marker)

    def test_ignores_processes_without_the_marker(self, marker, _cleanup) -> None:
        pid = _spawn_detached("setsid sleep 60 >/dev/null 2>&1 & echo $!", None)
        _cleanup.append(pid)

        assert pid not in find_tagged_pids(marker)

    def test_ignores_processes_tagged_with_another_marker(self, marker, _cleanup) -> None:
        pid = _spawn_detached("setsid sleep 60 >/dev/null 2>&1 & echo $!", uuid.uuid4().hex)
        _cleanup.append(pid)

        assert pid not in find_tagged_pids(marker)

    def test_never_returns_the_calling_process(self, marker, monkeypatch) -> None:
        monkeypatch.setenv(SANDBOX_MARKER_ENV, marker)
        # os.environ is not what /proc/self/environ shows after startup, but
        # even a tagged *caller* must be excluded by pid.
        assert os.getpid() not in find_tagged_pids(marker)

    def test_returns_nothing_when_proc_is_missing(self, marker, tmp_path) -> None:
        assert find_tagged_pids(marker, proc_root=str(tmp_path / "nope")) == []

    def test_a_marker_that_is_only_a_prefix_does_not_match(self, marker, _cleanup) -> None:
        pid = _spawn_detached("setsid sleep 60 >/dev/null 2>&1 & echo $!", marker + "extra")
        _cleanup.append(pid)

        assert pid not in find_tagged_pids(marker)


class TestReapTaggedProcesses:
    def test_kills_a_setsid_process(self, marker, _cleanup) -> None:
        pid = _spawn_detached("setsid sleep 60 >/dev/null 2>&1 & echo $!", marker)
        _cleanup.append(pid)

        killed = reap_tagged_processes(marker)

        assert pid in killed
        assert _wait_dead(pid)

    def test_kills_a_plain_background_grandchild(self, marker, _cleanup) -> None:
        pid = _spawn_detached("sleep 60 >/dev/null 2>&1 & echo $!", marker)
        _cleanup.append(pid)

        reap_tagged_processes(marker)

        assert _wait_dead(pid)

    def test_leaves_unrelated_processes_alone(self, marker, _cleanup) -> None:
        other = _spawn_detached("setsid sleep 60 >/dev/null 2>&1 & echo $!", uuid.uuid4().hex)
        untagged = _spawn_detached("setsid sleep 60 >/dev/null 2>&1 & echo $!", None)
        _cleanup.extend([other, untagged])

        assert reap_tagged_processes(marker) == []

        assert _alive(other)
        assert _alive(untagged)

    def test_is_idempotent(self, marker, _cleanup) -> None:
        pid = _spawn_detached("setsid sleep 60 >/dev/null 2>&1 & echo $!", marker)
        _cleanup.append(pid)
        reap_tagged_processes(marker)
        assert _wait_dead(pid)

        assert reap_tagged_processes(marker) == []

    def test_is_a_noop_without_proc(self, marker, tmp_path) -> None:
        assert reap_tagged_processes(marker, proc_root=str(tmp_path / "nope")) == []


# ── Node heap cap ──────────────────────────────────────────────────────

import shutil  # noqa: E402

from issue_agent.runner.guards import (  # noqa: E402
    NODE_HEAP_ENV,
    ensure_node_shim,
    node_guard_env,
    resolve_node_heap_mb,
)


def _meminfo(tmp_path: Path, total_kb: int) -> str:
    path = tmp_path / "meminfo"
    path.write_text(f"MemTotal:       {total_kb} kB\nMemFree:         1000 kB\n")
    return str(path)


class TestResolveNodeHeapMb:
    """The heap cap follows the VM: a share of ``MemTotal`` by default, so it
    grows by itself if the machine is resized."""

    def test_default_share_of_the_production_vm(self, tmp_path) -> None:
        # The Fly VM reports 985220 kB (~962 MB): 40% is ~384 MB.
        assert resolve_node_heap_mb(40, 0, meminfo_path=_meminfo(tmp_path, 985220)) == 384

    def test_scales_with_a_bigger_vm(self, tmp_path) -> None:
        # 2 GiB → 40% = 819 MB.
        assert resolve_node_heap_mb(40, 0, meminfo_path=_meminfo(tmp_path, 2 * 1024 * 1024)) == 819

    def test_explicit_megabytes_win_over_the_percentage(self, tmp_path) -> None:
        assert resolve_node_heap_mb(40, 700, meminfo_path=_meminfo(tmp_path, 985220)) == 700

    def test_explicit_megabytes_do_not_need_meminfo(self, tmp_path) -> None:
        assert resolve_node_heap_mb(40, 700, meminfo_path=str(tmp_path / "missing")) == 700

    def test_floor_keeps_a_tiny_vm_usable(self, tmp_path) -> None:
        # 256 MB VM × 40% = 102 MB would be unusable for any real toolchain.
        assert resolve_node_heap_mb(40, 0, meminfo_path=_meminfo(tmp_path, 256 * 1024)) == 128

    def test_zero_percent_and_zero_megabytes_disable_the_cap(self, tmp_path) -> None:
        assert resolve_node_heap_mb(0, 0, meminfo_path=_meminfo(tmp_path, 985220)) is None

    def test_unreadable_meminfo_means_no_cap(self, tmp_path) -> None:
        assert resolve_node_heap_mb(40, 0, meminfo_path=str(tmp_path / "missing")) is None

    def test_garbage_meminfo_means_no_cap(self, tmp_path) -> None:
        bad = tmp_path / "meminfo"
        bad.write_text("not a meminfo\n")
        assert resolve_node_heap_mb(40, 0, meminfo_path=str(bad)) is None

    def test_negative_values_are_rejected(self, tmp_path) -> None:
        with pytest.raises(ValueError):
            resolve_node_heap_mb(-1, 0, meminfo_path=_meminfo(tmp_path, 985220))
        with pytest.raises(ValueError):
            resolve_node_heap_mb(40, -5, meminfo_path=_meminfo(tmp_path, 985220))

    def test_percentage_above_100_is_rejected(self, tmp_path) -> None:
        with pytest.raises(ValueError):
            resolve_node_heap_mb(101, 0, meminfo_path=_meminfo(tmp_path, 985220))


class TestEnsureNodeShim:
    def test_writes_an_executable_node_wrapper(self, tmp_path) -> None:
        shim_dir = ensure_node_shim(tmp_path / "shims")

        shim = shim_dir / "node"
        assert shim.is_file()
        assert os.access(shim, os.X_OK)

    def test_is_idempotent(self, tmp_path) -> None:
        first = ensure_node_shim(tmp_path / "shims")
        content = (first / "node").read_text()
        mtime = (first / "node").stat().st_mtime_ns

        second = ensure_node_shim(tmp_path / "shims")

        assert second == first
        assert (second / "node").read_text() == content
        assert (second / "node").stat().st_mtime_ns == mtime

    def test_repairs_a_stale_or_non_executable_shim(self, tmp_path) -> None:
        shim = ensure_node_shim(tmp_path / "shims") / "node"
        shim.write_text("#!/bin/sh\necho stale\n")
        shim.chmod(0o644)

        ensure_node_shim(tmp_path / "shims")

        assert os.access(shim, os.X_OK)
        assert "stale" not in shim.read_text()

    def test_delegates_to_the_real_node_found_after_the_shim_dir(self, tmp_path) -> None:
        shim_dir = ensure_node_shim(tmp_path / "shims")
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        real = real_dir / "node"
        real.write_text('#!/bin/sh\nprintf "real:%s\\n" "$*"\n')
        real.chmod(0o755)
        env = {
            "PATH": f"{shim_dir}:{real_dir}:/usr/bin:/bin",
            NODE_HEAP_ENV: "256",
        }

        out = subprocess.run(
            ["node", "script.js", "--flag"], env=env, capture_output=True, text=True, check=True
        ).stdout

        assert out.strip() == "real:--max-old-space-size=256 script.js --flag"

    def test_fails_clearly_when_no_real_node_exists(self, tmp_path) -> None:
        shim_dir = ensure_node_shim(tmp_path / "shims")
        env = {"PATH": f"{shim_dir}:/usr/bin:/bin", NODE_HEAP_ENV: "256"}
        if shutil.which("node", path="/usr/bin:/bin"):
            pytest.skip("a system node exists in /usr/bin")

        proc = subprocess.run(["node", "-v"], env=env, capture_output=True, text=True)

        assert proc.returncode == 127
        assert "node" in proc.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="needs a real node")
class TestShimBeatsTheAgentsNodeOptions:
    """The agent set ``NODE_OPTIONS=--max-old-space-size=4096`` inline on a VM
    with 1 GB. A command-line flag overrides ``NODE_OPTIONS``, so the shim wins.

    V8's reported ``heap_size_limit`` is the old-space cap plus a young-generation
    allowance that depends on the Node version, so the expectation is measured
    from the same ``node`` with the flag given directly, never hard-coded.
    """

    _PROBE = "console.log(Math.round(require('v8').getHeapStatistics().heap_size_limit/1048576))"
    _CAP_MB = 128

    @pytest.fixture
    def expected(self) -> int:
        out = subprocess.run(
            ["node", f"--max-old-space-size={self._CAP_MB}", "-e", self._PROBE],
            capture_output=True, text=True, check=True,
        ).stdout
        limit = int(out.strip())
        uncapped = int(
            subprocess.run(
                ["node", "-e", self._PROBE], capture_output=True, text=True, check=True
            ).stdout.strip()
        )
        assert limit < uncapped, "sanity: the cap must actually lower V8's limit"
        return limit

    def _heap_limit_mb(self, tmp_path, shell_command: str) -> int:
        shim_dir = ensure_node_shim(tmp_path / "shims")
        env = {
            **os.environ,
            "PATH": f"{shim_dir}:{os.environ['PATH']}",
            NODE_HEAP_ENV: str(self._CAP_MB),
        }
        out = subprocess.run(
            ["sh", "-c", shell_command], env=env, capture_output=True, text=True, check=True
        ).stdout
        return int(out.strip())

    def test_inline_node_options_cannot_raise_the_cap(self, tmp_path, expected) -> None:
        limit = self._heap_limit_mb(
            tmp_path, f'NODE_OPTIONS="--max-old-space-size=4096" node -e "{self._PROBE}"'
        )

        assert limit == expected

    def test_exported_node_options_cannot_raise_the_cap(self, tmp_path, expected) -> None:
        limit = self._heap_limit_mb(
            tmp_path,
            f'export NODE_OPTIONS="--max-old-space-size=4096"; node -e "{self._PROBE}"',
        )

        assert limit == expected

    def test_cap_reaches_node_launched_through_a_script_shebang(self, tmp_path, expected) -> None:
        script = tmp_path / "tool.js"
        script.write_text(f"#!/usr/bin/env node\n{self._PROBE}\n")
        script.chmod(0o755)

        assert self._heap_limit_mb(tmp_path, str(script)) == expected

    def test_without_the_shim_the_agents_node_options_would_win(self, tmp_path) -> None:
        """Guards the premise: this is the escape the shim exists to close."""
        out = subprocess.run(
            ["sh", "-c", f'NODE_OPTIONS="--max-old-space-size=4096" node -e "{self._PROBE}"'],
            capture_output=True, text=True, check=True,
        ).stdout

        assert int(out.strip()) > 2048


class TestNodeGuardEnv:
    def test_puts_the_shim_first_and_keeps_the_rest_of_the_path(self, tmp_path) -> None:
        env = node_guard_env({"PATH": "/data/tools/nodejs/20/bin:/usr/bin"}, tmp_path / "shims", 384)

        assert env["PATH"] == f"{tmp_path / 'shims'}:/data/tools/nodejs/20/bin:/usr/bin"
        assert env[NODE_HEAP_ENV] == "384"
        assert (tmp_path / "shims" / "node").is_file()

    def test_falls_back_to_the_worker_path_when_the_env_has_none(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")

        env = node_guard_env({}, tmp_path / "shims", 256)

        assert env["PATH"] == f"{tmp_path / 'shims'}:/usr/local/bin:/usr/bin"

    def test_keeps_other_entries_and_does_not_mutate_the_input(self, tmp_path) -> None:
        original = {"PATH": "/x", "NODE_PATH": "/lib"}

        env = node_guard_env(original, tmp_path / "shims", 256)

        assert env["NODE_PATH"] == "/lib"
        assert original == {"PATH": "/x", "NODE_PATH": "/lib"}

    def test_without_a_cap_the_env_is_returned_untouched(self, tmp_path) -> None:
        original = {"PATH": "/x"}

        env = node_guard_env(original, tmp_path / "shims", None)

        assert env == original
        assert not (tmp_path / "shims").exists()
