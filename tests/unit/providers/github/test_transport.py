from __future__ import annotations

import base64

import pytest

from issue_agent.providers.github.transport import git_auth_env

_HOST = "https://github.com"


class TestGitAuthEnv:
    def test_returns_git_config_override(self) -> None:
        env = git_auth_env("ghs_abc", host=_HOST)

        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com.extraHeader"

        expected_cred = base64.b64encode(b"x-access-token:ghs_abc").decode()
        assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {expected_cred}"

    def test_header_is_scoped_to_the_given_host(self) -> None:
        env = git_auth_env("ghs_abc", host="https://example.internal")
        assert env["GIT_CONFIG_KEY_0"] == "http.https://example.internal.extraHeader"

    def test_host_is_required(self) -> None:
        # No default: a call site that forgets it would only ever authenticate
        # github.com.
        with pytest.raises(TypeError):
            git_auth_env("ghs_abc")  # type: ignore[call-arg]

    def test_disables_terminal_prompt(self) -> None:
        env = git_auth_env("ghs_abc", host=_HOST)
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_different_tokens_produce_different_headers(self) -> None:
        env_a = git_auth_env("token-a", host=_HOST)
        env_b = git_auth_env("token-b", host=_HOST)
        assert env_a["GIT_CONFIG_VALUE_0"] != env_b["GIT_CONFIG_VALUE_0"]

    def test_token_not_present_verbatim_in_any_value(self) -> None:
        token = "ghs_super_secret_token"
        env = git_auth_env(token, host=_HOST)
        assert all(token not in v for v in env.values())
