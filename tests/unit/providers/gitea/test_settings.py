"""``GiteaProviderSettings`` and its place in the ``providers.*`` union."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings, flatten_yaml_config
from issue_agent.providers.gitea.settings import GiteaProviderSettings
from issue_agent.providers.github.settings import GitHubProviderSettings
from issue_agent.providers.settings import SETTINGS_BY_TYPE


class SettingsNoEnv(Settings):
    """No real .env or config.yaml; CONFIG_FILE points at a test file."""

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        extra="ignore",
        yaml_file=None,
        env_nested_delimiter="__",
    )


def _gitea(**fields: Any) -> GiteaProviderSettings:
    return GiteaProviderSettings.model_validate({"type": "gitea", **fields})


class TestModel:
    def test_defaults(self):
        config = _gitea()

        assert config.host == ""
        assert config.bot_login == ""
        assert config.author_permission == "allowlist"
        assert config.ca_bundle_path == ""
        assert config.request_timeout_seconds == 30.0
        assert config.token == ""
        assert config.webhook_secret == ""

    def test_secret_fields(self):
        assert GiteaProviderSettings.secret_fields == {"token", "webhook_secret"}

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://git.example.com", "https://git.example.com"),
            ("https://git.example.com/", "https://git.example.com"),
            ("  http://git.local:3000/ ", "http://git.local:3000"),
            ("https://example.com/git/", "https://example.com/git"),
            ("", ""),
        ],
    )
    def test_host_is_normalized(self, raw: str, expected: str):
        assert _gitea(host=raw).host == expected

    @pytest.mark.parametrize("raw", ["git.example.com", "ftp://git.example.com", "//git"])
    def test_host_needs_a_scheme(self, raw: str):
        with pytest.raises(ValidationError, match="must include the scheme"):
            _gitea(host=raw)

    def test_api_url_keeps_a_subpath(self):
        assert _gitea(host="https://example.com/git/").api_url == "https://example.com/git/api/v1"

    def test_strips_bot_login_and_ca_bundle_path(self):
        config = _gitea(bot_login=" issue-agent ", ca_bundle_path=" /data/ca.pem ")

        assert config.bot_login == "issue-agent"
        assert config.ca_bundle_path == "/data/ca.pem"

    def test_author_permission_is_a_closed_set(self):
        assert _gitea(author_permission="api").author_permission == "api"
        with pytest.raises(ValidationError):
            _gitea(author_permission="admin")

    @pytest.mark.parametrize("timeout", [0, -1])
    def test_timeout_must_be_positive(self, timeout: float):
        with pytest.raises(ValidationError):
            _gitea(request_timeout_seconds=timeout)

    @pytest.mark.parametrize("field", ["verify_tls", "app_id"])
    def test_unknown_fields_are_rejected(self, field: str):
        with pytest.raises(ValidationError, match="Extra inputs"):
            _gitea(**{field: False})

    def test_imports_only_pydantic_and_the_ports(self):
        # Loading the config must not pull in the HTTP client (§4.7).
        import issue_agent.providers.gitea.settings as module

        tree = ast.parse(Path(module.__file__).read_text())
        imported = {
            node.module.split(".")[0]
            if node.module != "issue_agent.providers.base"
            else node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        } | {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }

        assert imported <= {"__future__", "typing", "pydantic", "issue_agent.providers.base"}


class TestUnion:
    def test_registered_by_type(self):
        assert SETTINGS_BY_TYPE["gitea"] is GiteaProviderSettings

    def test_yaml_secrets_are_rejected_with_the_env_name(self):
        with pytest.raises(ValueError, match="PROVIDERS__GITEA_CORP__TOKEN"):
            flatten_yaml_config({"providers": {"gitea_corp": {"type": "gitea", "token": "x"}}})
        with pytest.raises(ValueError, match="PROVIDERS__GITEA__WEBHOOK_SECRET"):
            flatten_yaml_config({"providers": {"gitea": {"type": "gitea", "webhook_secret": "x"}}})

    def test_non_secret_fields_are_fine_in_yaml(self):
        flat = flatten_yaml_config(
            {"providers": {"gitea": {"type": "gitea", "host": "https://git.example.com"}}}
        )

        assert flat["PROVIDERS"]["gitea"]["host"] == "https://git.example.com"


class TestLoadedConfig:
    def _load(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, yaml: str) -> Settings:
        path = tmp_path / "config.yaml"
        path.write_text(textwrap.dedent(yaml))
        monkeypatch.setenv("CONFIG_FILE", str(path))
        return SettingsNoEnv(LLM_API_KEY="k")

    def test_yaml_instance_merges_env_secrets(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROVIDERS__GITEA_CORP__TOKEN", "tok")
        monkeypatch.setenv("PROVIDERS__GITEA_CORP__WEBHOOK_SECRET", "whs")
        settings = self._load(
            tmp_path,
            monkeypatch,
            """
            providers:
              gitea_corp:
                type: gitea
                host: https://git.example.com/
                bot_login: issue-agent
                author_permission: api
            repos:
              - issues: team/app
            """,
        )

        config = settings.providers["gitea_corp"]
        assert isinstance(config, GiteaProviderSettings)
        assert config.host == "https://git.example.com"
        assert config.bot_login == "issue-agent"
        assert config.author_permission == "api"
        assert config.token == "tok"
        assert config.webhook_secret == "whs"
        assert settings.route_for("gitea_corp", "team/app") is not None

    def test_coexists_with_a_github_instance(self, tmp_path, monkeypatch):
        settings = self._load(
            tmp_path,
            monkeypatch,
            """
            providers:
              github:
                type: github
                app_id: 1
              gitea:
                type: gitea
                host: https://git.example.com
            repos:
              - issues: org/repo
                provider: github
              - issues: team/app
                provider: gitea
            """,
        )

        assert isinstance(settings.providers["github"], GitHubProviderSettings)
        assert isinstance(settings.providers["gitea"], GiteaProviderSettings)
        assert settings.route_for("gitea", "team/app") is not None
        assert settings.route_for("gitea", "org/repo") is None

    def test_invalid_gitea_field_fails_the_load(self, tmp_path, monkeypatch):
        with pytest.raises(ValidationError, match="must include the scheme"):
            self._load(
                tmp_path,
                monkeypatch,
                """
                providers:
                  gitea:
                    type: gitea
                    host: git.example.com
                """,
            )
