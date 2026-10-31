from __future__ import annotations

from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.policy.comment_filter import comment_author_allowed
from tests.helpers.factories import make_author
from tests.helpers.settings import github_providers


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


def _settings(**overrides) -> Settings:
    base = dict(
        PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
        LLM_API_KEY="k",
    )
    base.update(overrides)
    return SettingsNoEnv(**base)


def _allowed(
    *,
    author_login="third-party",
    author_association="none",
    issue_author_login="reporter",
    is_self=False,
    settings=None,
) -> bool:
    return comment_author_allowed(
        author=make_author(
            login=author_login, association=author_association, is_self=is_self
        ),
        issue_author_login=issue_author_login,
        settings=settings or _settings(),
    )


class TestDisabled:
    def test_disabled_filter_allows_anyone(self):
        s = _settings(COMMENTS_AUTHOR_FILTER_ENABLED=False)
        assert _allowed(author_login="rando", settings=s) is True


class TestIssueAuthor:
    def test_issue_author_passes(self):
        assert _allowed(author_login="reporter", issue_author_login="reporter") is True

    def test_issue_author_match_is_case_insensitive(self):
        assert _allowed(author_login="Reporter", issue_author_login="reporter") is True

    def test_issue_author_rejected_when_flag_off(self):
        s = _settings(COMMENTS_AUTHOR_FILTER_ALLOW_ISSUE_AUTHOR=False)
        assert (
            _allowed(
                author_login="reporter",
                issue_author_login="reporter",
                author_association="none",
                settings=s,
            )
            is False
        )

    def test_bot_issue_author_passes_despite_none_association(self):
        # The reporter is a bot (e.g. a monitoring integration). Its follow-up
        # comments must keep reaching the agent for as long as the issue is
        # open — a maintainer may reopen it (R2.1, user directive).
        assert (
            _allowed(
                author_login="watchdog[bot]",
                author_association="none",
                issue_author_login="watchdog[bot]",
            )
            is True
        )


class TestAssociation:
    def test_owner_member_collaborator_pass(self):
        for assoc in ("owner", "member", "collaborator"):
            assert _allowed(author_association=assoc) is True, assoc

    def test_config_associations_match_case_insensitively(self):
        s = _settings(COMMENTS_AUTHOR_FILTER_ALLOW_ASSOCIATIONS=["Contributor"])
        assert _allowed(author_association="contributor", settings=s) is True

    def test_contributor_and_none_rejected(self):
        for assoc in ("contributor", "none", "bot"):
            assert _allowed(author_association=assoc) is False, assoc


class TestUnknownAssociation:
    """A forge that doesn't report associations (PRD §3.1)."""

    def test_unknown_never_matches_an_association(self):
        s = _settings(COMMENTS_AUTHOR_FILTER_ALLOW_ASSOCIATIONS=["unknown"])
        assert _allowed(author_association="unknown", settings=s) is False

    def test_unknown_issue_author_passes(self):
        assert (
            _allowed(
                author_login="reporter",
                author_association="unknown",
                issue_author_login="reporter",
            )
            is True
        )

    def test_unknown_allowlisted_login_passes(self):
        s = _settings(COMMENTS_AUTHOR_FILTER_ALLOWLIST=["maint"])
        assert (
            _allowed(author_login="maint", author_association="unknown", settings=s)
            is True
        )


class TestOwnComment:
    def test_own_comment_never_allowed(self):
        assert _allowed(author_association="member", is_self=True) is False

    def test_own_comment_rejected_even_with_filter_disabled(self):
        s = _settings(COMMENTS_AUTHOR_FILTER_ENABLED=False)
        assert _allowed(is_self=True, settings=s) is False


class TestAllowlist:
    def test_allowlisted_login_passes(self):
        s = _settings(COMMENTS_AUTHOR_FILTER_ALLOWLIST=["dependabot[bot]"])
        assert (
            _allowed(
                author_login="dependabot[bot]",
                author_association="none",
                settings=s,
            )
            is True
        )

    def test_allowlist_match_is_case_insensitive(self):
        s = _settings(COMMENTS_AUTHOR_FILTER_ALLOWLIST=["SomeBot[bot]"])
        assert _allowed(author_login="somebot[bot]", settings=s) is True


class TestRejection:
    def test_unrelated_third_party_rejected(self):
        assert _allowed(author_login="drive-by", author_association="none") is False

    def test_empty_author_login_rejected_without_error(self):
        assert _allowed(author_login="", issue_author_login="") is False
