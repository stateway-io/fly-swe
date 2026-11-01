from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from github import Github
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.providers.github.auth import GitHubAppAuth
from tests.helpers.settings import github_providers


class SettingsNoEnv(Settings):
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
        REPO_ROUTES=[
            {
                "issues": "stateway-io/stateway-issues",
                "target": "stateway-io/stateway-agent-fixtures",
            }
        ],
    )
    defaults.update(overrides)
    return SettingsNoEnv(**defaults)


@pytest.fixture
def settings() -> Settings:
    return _make_settings()


def _make_token(token: str, expires_in: timedelta) -> MagicMock:
    auth = MagicMock()
    auth.token = token
    auth.expires_at = datetime.now(UTC) + expires_in
    auth.permissions = {}
    return auth


@pytest.fixture
def mock_auth() -> MagicMock:
    return _make_token("ghs_installation_token", timedelta(hours=1))


@pytest.fixture
def mock_integration(mock_auth: MagicMock) -> MagicMock:
    with patch("issue_agent.providers.github.auth.GithubIntegration") as mi:
        inst = mi.return_value
        inst.get_access_token.return_value = mock_auth
        yield inst


class TestGitHubAppAuthPerInstallationCache:
    def test_get_token_returns_token(self, settings: Settings, mock_integration: MagicMock) -> None:
        auth = GitHubAppAuth(settings.providers["github"])
        token = auth.get_token(111)
        assert token == "ghs_installation_token"
        mock_integration.get_access_token.assert_called_once_with(111)

    def test_get_token_caches_per_installation(
        self, settings: Settings, mock_integration: MagicMock
    ) -> None:
        auth = GitHubAppAuth(settings.providers["github"])
        t1 = auth.get_token(111)
        t2 = auth.get_token(111)
        assert t1 == t2 == "ghs_installation_token"
        mock_integration.get_access_token.assert_called_once_with(111)

    def test_independent_caches_for_different_installations(
        self, settings: Settings, mock_integration: MagicMock
    ) -> None:
        """get_token(111) and get_token(222) must not share a cache slot —
        two distinct _fetch_token calls, each returning its own token,
        neither invalidating the other."""
        token_111 = _make_token("ghs_token_111", timedelta(hours=1))
        token_222 = _make_token("ghs_token_222", timedelta(hours=1))
        mock_integration.get_access_token.side_effect = lambda installation_id: (
            token_111 if installation_id == 111 else token_222
        )

        auth = GitHubAppAuth(settings.providers["github"])

        assert auth.get_token(111) == "ghs_token_111"
        assert auth.get_token(222) == "ghs_token_222"
        # Both cached independently — no re-fetch on repeat calls.
        assert auth.get_token(111) == "ghs_token_111"
        assert auth.get_token(222) == "ghs_token_222"

        assert mock_integration.get_access_token.call_count == 2
        mock_integration.get_access_token.assert_any_call(111)
        mock_integration.get_access_token.assert_any_call(222)

    @pytest.mark.parametrize("installation_id", [111, 222])
    def test_get_token_refreshes_when_expired(
        self, settings: Settings, mock_integration: MagicMock, installation_id: int
    ) -> None:
        mock_integration.get_access_token.return_value = _make_token("t", timedelta(minutes=2))
        auth = GitHubAppAuth(settings.providers["github"])
        auth.get_token(installation_id)
        auth.get_token(installation_id)
        assert mock_integration.get_access_token.call_count == 2

    @pytest.mark.parametrize("installation_id", [111, 222])
    def test_get_token_does_not_refresh_when_far_from_expiry(
        self, settings: Settings, mock_integration: MagicMock, installation_id: int
    ) -> None:
        mock_integration.get_access_token.return_value = _make_token("t", timedelta(minutes=30))
        auth = GitHubAppAuth(settings.providers["github"])
        auth.get_token(installation_id)
        auth.get_token(installation_id)
        mock_integration.get_access_token.assert_called_once_with(installation_id)

    def test_get_client_returns_github_instance(
        self, settings: Settings, mock_integration: MagicMock
    ) -> None:
        auth = GitHubAppAuth(settings.providers["github"])
        client = auth.get_client(111)
        assert isinstance(client, Github)

    def test_get_client_uses_cached_token(
        self, settings: Settings, mock_integration: MagicMock
    ) -> None:
        auth = GitHubAppAuth(settings.providers["github"])
        auth.get_client(111)
        auth.get_client(111)
        mock_integration.get_access_token.assert_called_once_with(111)

    def test_fetch_token_updates_cache(
        self, settings: Settings, mock_integration: MagicMock
    ) -> None:
        auth = GitHubAppAuth(settings.providers["github"])
        token1 = auth._fetch_token(111)
        assert token1 == "ghs_installation_token"
        assert auth._cache[111].token == "ghs_installation_token"

    def test_get_token_propagates_exception(
        self, settings: Settings, mock_integration: MagicMock
    ) -> None:
        mock_integration.get_access_token.side_effect = Exception("API error")
        auth = GitHubAppAuth(settings.providers["github"])
        with pytest.raises(Exception, match="API error"):
            auth.get_token(111)


class TestResolveInstallationId:
    def test_resolve_installation_id_returns_id(self, settings: Settings) -> None:
        with patch("issue_agent.providers.github.auth.GithubIntegration") as mi:
            mi.return_value.get_repo_installation.return_value = MagicMock(id=999)
            auth = GitHubAppAuth(settings.providers["github"])
            result = auth.resolve_installation_id("owner", "repo")
        assert result == 999
        mi.return_value.get_repo_installation.assert_called_once_with("owner", "repo")

    def test_resolve_installation_id_propagates_exception(self, settings: Settings) -> None:
        with patch("issue_agent.providers.github.auth.GithubIntegration") as mi:
            mi.return_value.get_repo_installation.side_effect = Exception("not found")
            auth = GitHubAppAuth(settings.providers["github"])
            with pytest.raises(Exception, match="not found"):
                auth.resolve_installation_id("owner", "repo")


class TestApiBaseUrl:
    @pytest.mark.parametrize(
        ("host", "api_url"),
        [
            ("https://github.com", "https://api.github.com"),
            ("https://ghe.example.com", "https://ghe.example.com/api/v3"),
        ],
    )
    def test_integration_and_client_talk_to_the_configured_instance(
        self, host: str, api_url: str, mock_auth: MagicMock
    ) -> None:
        with (
            patch("issue_agent.providers.github.auth.GithubIntegration") as mi,
            patch("issue_agent.providers.github.auth.Github") as mg,
        ):
            mi.return_value.get_access_token.return_value = mock_auth
            auth = GitHubAppAuth(
                _make_settings(PROVIDERS=github_providers(host=host)).providers["github"]
            )
            auth.get_client(111)

        assert mi.call_args.kwargs["base_url"] == api_url
        assert mg.call_args.kwargs["base_url"] == api_url


class TestInstallationForRepo:
    """Each repo resolves its own installation (PRD §2.2) — once per process."""

    def test_resolves_from_the_repo_slug(self, settings: Settings) -> None:
        with patch("issue_agent.providers.github.auth.GithubIntegration") as mi:
            mi.return_value.get_repo_installation.return_value = MagicMock(id=42)
            auth = GitHubAppAuth(settings.providers["github"])

            assert auth.installation_for("org/repo") == 42
        mi.return_value.get_repo_installation.assert_called_once_with("org", "repo")

    def test_cached_per_repo(self, settings: Settings) -> None:
        with patch("issue_agent.providers.github.auth.GithubIntegration") as mi:
            mi.return_value.get_repo_installation.side_effect = [
                MagicMock(id=1),
                MagicMock(id=2),
            ]
            auth = GitHubAppAuth(settings.providers["github"])

            assert auth.installation_for("org/issues") == 1
            assert auth.installation_for("org/issues") == 1
            assert auth.installation_for("other/target") == 2
        assert mi.return_value.get_repo_installation.call_count == 2

    def test_token_for_uses_the_repos_installation(
        self, settings: Settings, mock_integration: MagicMock
    ) -> None:
        mock_integration.get_repo_installation.return_value = MagicMock(id=7)
        auth = GitHubAppAuth(settings.providers["github"])

        assert auth.token_for("org/repo") == "ghs_installation_token"
        mock_integration.get_access_token.assert_called_once_with(7)
