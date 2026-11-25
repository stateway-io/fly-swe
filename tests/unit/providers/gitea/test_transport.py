"""``GiteaTransport``: URLs, references and the git credential env."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from issue_agent.providers.gitea.transport import GiteaTransport

if TYPE_CHECKING:
    from pathlib import Path

TOKEN = "gitea-token-value"


def _transport(host: str = "https://git.example.com", **kw: str) -> GiteaTransport:
    return GiteaTransport(host, bot_login="issue-agent", token=TOKEN, **kw)


class TestUrls:
    @pytest.mark.parametrize(
        "host", ["https://git.example.com", "https://example.com/git", "http://gitea:3000"]
    )
    def test_built_from_the_host(self, host: str):
        t = _transport(host)

        assert t.clone_url("team/app") == f"{host}/team/app.git"
        assert t.pr_url("team/app", 7) == f"{host}/team/app/pulls/7"
        assert t.compare_url("team/app", "main", "agent/issue-1-x") == (
            f"{host}/team/app/compare/main...agent/issue-1-x"
        )

    def test_clone_url_carries_no_credential(self):
        assert TOKEN not in _transport().clone_url("team/app")
        assert "@" not in _transport().clone_url("team/app")

    def test_compare_url_quotes_branch_names(self):
        url = _transport().compare_url("team/app", "release 1", "agent/fix#2?x")

        assert url.endswith("/compare/release%201...agent/fix%232%3Fx")


class TestReferences:
    def test_same_repo(self):
        t = _transport()

        assert t.format_ref("issue", 3, repo="team/app", from_repo="team/app") == "#3"
        assert t.format_ref("pr", 4, repo="team/app", from_repo="team/app") == "#4"
        assert t.closing_reference("team/app", 3, "team/app") == "Closes #3"

    def test_cross_repo(self):
        t = _transport()

        assert t.format_ref("issue", 3, repo="team/issues", from_repo="team/app") == (
            "team/issues#3"
        )
        assert t.closing_reference("team/issues", 3, "team/app") == "Closes team/issues#3"


@pytest.mark.parametrize(
    ("host", "email"),
    [
        ("https://git.example.com", "bot@noreply.git.example.com"),
        ("http://gitea:3000", "bot@noreply.gitea"),
        ("https://example.com/git", "bot@noreply.example.com"),
    ],
)
def test_default_commit_email(host: str, email: str):
    assert _transport(host).default_commit_email("bot") == email


class TestGitAuthEnv:
    def test_basic_header_scoped_to_the_host(self):
        env = _transport("https://example.com/git").git_auth_env("team/app")
        expected = base64.b64encode(f"issue-agent:{TOKEN}".encode()).decode()

        assert env == {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://example.com/git/.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {expected}",
            "GIT_TERMINAL_PROMPT": "0",
        }

    def test_ca_bundle_adds_ssl_ca_info_for_the_host(self):
        env = _transport(ca_bundle_path="/data/ca.pem").git_auth_env("team/app")

        assert env["GIT_CONFIG_COUNT"] == "2"
        assert env["GIT_CONFIG_KEY_1"] == "http.https://git.example.com/.sslCAInfo"
        assert env["GIT_CONFIG_VALUE_1"] == "/data/ca.pem"


def _urlmatch(env: dict[str, str], key: str, url: str, home: Path) -> str | None:
    """What git itself resolves for ``key`` at ``url`` under ``env``."""
    result = subprocess.run(
        ["git", "config", "--get-urlmatch", key, url],
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None


@pytest.mark.skipif(shutil.which("git") is None, reason="needs the git binary")
class TestGitResolvesTheEnv:
    """The env is only right if git reads it the way we mean: the header for
    the instance's repos, and nothing for any other URL."""

    def test_header_applies_under_the_host_subpath_only(self, tmp_path: Path):
        env = _transport("https://example.com/git").git_auth_env("team/app")

        header = _urlmatch(
            env, "http.extraHeader", "https://example.com/git/team/app.git", tmp_path
        )
        assert header is not None
        assert header.startswith("Authorization: Basic ")
        for other in (
            "https://example.com/other/team/app.git",
            "https://example.com/gitx/team/app.git",
            "https://evil.example/git/team/app.git",
            "http://example.com/git/team/app.git",
        ):
            assert _urlmatch(env, "http.extraHeader", other, tmp_path) is None, other

    def test_ca_bundle_applies_to_the_host(self, tmp_path: Path):
        env = _transport(ca_bundle_path="/data/ca.pem").git_auth_env("team/app")

        assert (
            _urlmatch(env, "http.sslCAInfo", "https://git.example.com/team/app.git", tmp_path)
            == "/data/ca.pem"
        )
        assert _urlmatch(env, "http.sslCAInfo", "https://github.com/a/b.git", tmp_path) is None
