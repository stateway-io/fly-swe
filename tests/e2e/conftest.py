from __future__ import annotations

import os

import pytest
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings

_REQUIRED_E2E_VARS = [
    "PROVIDERS__GITHUB__APP_ID",
    "PROVIDERS__GITHUB__WEBHOOK_SECRET",
    "LLM_API_KEY",
]
_KEY_VARS = ("PROVIDERS__GITHUB__APP_PRIVATE_KEY", "PROVIDERS__GITHUB__APP_PRIVATE_KEY_PATH")


class E2ESettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


@pytest.fixture
def e2e_test_repo() -> str:
    """Repo used by E2E tests — test-only config, not part of Settings (R5.1.6)."""
    return os.environ.get("E2E_TEST_REPO", "stateway-io/stateway-issues")


@pytest.fixture
def e2e_settings() -> Settings:
    missing = [v for v in _REQUIRED_E2E_VARS if not os.environ.get(v, "")]
    if not any(os.environ.get(v, "") for v in _KEY_VARS):
        missing.append(" or ".join(_KEY_VARS))
    if missing:
        pytest.skip(
            f"E2E tests require env vars: {', '.join(missing)}. "
            "Set them to run tests marked with @pytest.mark.e2e."
        )

    # The instance's fields (and secrets) come from PROVIDERS__GITHUB__* in
    # the env and deep-merge into this entry; only the type is given here.
    return E2ESettingsNoEnv(
        PROVIDERS={"github": {"type": "github"}},
        LLM_API_KEY=os.environ["LLM_API_KEY"],
        REPO_ROUTES=[
            {
                "issues": os.environ.get("E2E_TEST_REPO", "stateway-io/stateway-issues"),
                "target": os.environ.get("E2E_TARGET_REPO", "stateway-io/stateway-agent-fixtures"),
            }
        ],
        MAX_ITERATIONS=int(os.environ.get("MAX_ITERATIONS", "50")),
        MAX_BUDGET_PER_TASK_USD=float(os.environ.get("MAX_BUDGET_PER_TASK_USD", "2.00")),
        LOG_LEVEL=os.environ.get("LOG_LEVEL", "info"),
    )
