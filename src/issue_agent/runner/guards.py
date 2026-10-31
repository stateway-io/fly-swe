"""Containment of what the coding agent leaves running on the Fly VM.

The VM is small (1 shared vCPU, ~1 GB, no swap). The agent's shell is
sandboxed per command — ``_run`` kills the command's process group on timeout —
but a process that detaches itself (``setsid``, ``nohup … &``) leaves that
group, outlives the command and can pile ``tsc`` and ``jest`` onto the VM until
it thrashes. Everything the agent starts carries a per-environment marker in
its environment, which descendants inherit even after ``setsid`` and
re-parenting to PID 1, so a scan of ``/proc/<pid>/environ`` finds them all
without privileges or cgroups.
"""

from __future__ import annotations

import os
import signal
import time
from contextlib import suppress
from pathlib import Path

# Injected into every command's environment by ``SandboxedLocalEnvironment``.
SANDBOX_MARKER_ENV = "ISSUE_AGENT_SANDBOX"

# Read by the ``node`` shim: the V8 old-space cap, in MB.
NODE_HEAP_ENV = "ISSUE_AGENT_NODE_MAX_OLD_SPACE_MB"
# Below this a Node toolchain (tsc, jest) cannot do useful work at all.
_NODE_HEAP_FLOOR_MB = 128

_REAP_PASSES = 3
_REAP_SETTLE_SECONDS = 0.05


def find_tagged_pids(marker: str, proc_root: str = "/proc") -> list[int]:
    """Pids whose environment carries ``ISSUE_AGENT_SANDBOX=<marker>``.

    Never returns the calling process. Unreadable entries (a process that just
    exited, a zombie, another user's) are skipped; with no ``/proc`` (macOS in
    development) the result is empty, so containment degrades to a no-op.
    """
    needle = f"{SANDBOX_MARKER_ENV}={marker}".encode()
    me = os.getpid()
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return []
    pids: list[int] = []
    for entry in entries:
        if not entry.isdigit() or int(entry) == me:
            continue
        try:
            environ = (Path(proc_root) / entry / "environ").read_bytes()
        except OSError:
            continue
        if needle in environ.split(b"\0"):
            pids.append(int(entry))
    return pids


def reap_tagged_processes(marker: str, proc_root: str = "/proc") -> list[int]:
    """SIGKILL every process still tagged with ``marker``; return the pids killed.

    Several passes, because a process killed in one pass can have forked just
    before dying. Idempotent: with nothing left it returns ``[]``.
    """
    killed: list[int] = []
    for _ in range(_REAP_PASSES):
        pids = find_tagged_pids(marker, proc_root)
        if not pids:
            break
        for pid in pids:
            with suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
            if pid not in killed:
                killed.append(pid)
        time.sleep(_REAP_SETTLE_SECONDS)
    return killed


def resolve_node_heap_mb(
    percent: int, mb_override: int, meminfo_path: str = "/proc/meminfo"
) -> int | None:
    """V8 old-space cap, in MB, for the agent's Node processes — or ``None`` for no cap.

    An explicit ``mb_override`` wins. Otherwise ``percent`` of the VM's
    ``MemTotal``, so the cap follows the machine if it is ever resized (40% of
    the 1 GB production VM is ~384 MB). Never below ``_NODE_HEAP_FLOOR_MB``.
    ``percent == 0`` with no override, or a ``MemTotal`` that cannot be read
    (macOS in development), means no cap.
    """
    if mb_override < 0 or not 0 <= percent <= 100:
        raise ValueError(
            f"invalid node heap settings: percent={percent} (0-100), mb={mb_override} (>= 0)"
        )
    if mb_override > 0:
        return mb_override
    if percent == 0:
        return None
    try:
        with open(meminfo_path, encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total_mb = int(line.split()[1]) / 1024
                    return max(_NODE_HEAP_FLOOR_MB, int(total_mb * percent / 100))
    except (OSError, ValueError, IndexError):
        return None
    return None


# A command-line flag overrides NODE_OPTIONS, so a `node` that always passes
# --max-old-space-size also beats the agent's inline
# `NODE_OPTIONS="--max-old-space-size=4096" npx jest`. The real node is looked up
# on the PATH *minus the shim's own directory*, but the child keeps the full
# PATH, so node processes spawned further down (jest workers, npm scripts) hit
# the shim too. A `node` invoked by absolute path is not covered.
_NODE_SHIM = """#!/bin/sh
# Installed by issue-agent (runner/guards.py) - do not edit.
self_dir=$(cd "$(dirname "$0")" && pwd -P)
rest=""
oldifs=$IFS
IFS=:
for d in $PATH; do
  [ "$(cd "$d" 2>/dev/null && pwd -P)" = "$self_dir" ] && continue
  rest="${rest:+$rest:}$d"
done
IFS=$oldifs
real=$(PATH="$rest" command -v node) || { echo "node: command not found" >&2; exit 127; }
exec "$real" ${ISSUE_AGENT_NODE_MAX_OLD_SPACE_MB:+--max-old-space-size="$ISSUE_AGENT_NODE_MAX_OLD_SPACE_MB"} "$@"
"""


def ensure_node_shim(shim_dir: Path | str) -> Path:
    """Write the ``node`` shim into ``shim_dir`` (idempotent) and return the dir."""
    directory = Path(shim_dir)
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "node"
    if not (shim.is_file() and os.access(shim, os.X_OK) and shim.read_text() == _NODE_SHIM):
        shim.write_text(_NODE_SHIM)
        shim.chmod(0o755)
    return directory


def node_guard_env(
    env: dict[str, str], shim_dir: Path | str, heap_mb: int | None
) -> dict[str, str]:
    """``env`` with the capped ``node`` shim first on ``PATH``; unchanged without a cap."""
    if heap_mb is None:
        return dict(env)
    directory = ensure_node_shim(shim_dir)
    path = env.get("PATH") or os.environ.get("PATH", "")
    guarded = dict(env)
    guarded["PATH"] = f"{directory}{os.pathsep}{path}" if path else str(directory)
    guarded[NODE_HEAP_ENV] = str(heap_mb)
    return guarded
