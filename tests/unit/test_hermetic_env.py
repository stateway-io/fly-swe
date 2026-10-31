"""The test suite must not read the developer's `.env` through litellm."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Counts the `dotenv.load_dotenv` calls made while litellm is imported.
_PROBE = """
import dotenv
calls = []
dotenv.load_dotenv = lambda *a, **k: calls.append(1)
{setup}
import litellm
print(len(calls))
"""


def _load_dotenv_calls(setup: str) -> int:
    env = {k: v for k, v in os.environ.items() if k != "LITELLM_MODE"}
    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(setup=setup)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return int(result.stdout.strip().splitlines()[-1])


def test_litellm_loads_dotenv_by_default() -> None:
    # Guards the probe itself: if litellm stops calling load_dotenv, the
    # conftest workaround (and the test below) can go.
    assert _load_dotenv_calls(setup="") > 0


def test_root_conftest_stops_litellm_from_loading_dotenv() -> None:
    assert _load_dotenv_calls(setup="import tests.conftest") == 0
