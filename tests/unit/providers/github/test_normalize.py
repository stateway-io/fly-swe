from __future__ import annotations

import pytest

from issue_agent.providers.github.normalize import (
    BotIdentity,
    author_from_github,
    comment_dict_is_own_app,
    map_association,
)

APP_ID = 3735121
IDENTITY = BotIdentity(app_id=APP_ID, app_name="Stateway Issue Agent")


class TestMapAssociation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("OWNER", "owner"),
            ("MEMBER", "member"),
            ("COLLABORATOR", "collaborator"),
            ("CONTRIBUTOR", "contributor"),
            ("FIRST_TIME_CONTRIBUTOR", "contributor"),
            ("FIRST_TIMER", "none"),
            ("MANNEQUIN", "none"),
            ("NONE", "none"),
            (" member ", "member"),
            ("owner", "owner"),
            (None, "unknown"),
            ("", "unknown"),
            ("SOMETHING_NEW", "unknown"),
        ],
    )
    def test_mapping(self, raw, expected):
        assert map_association(raw) == expected


class TestAuthorFromGithub:
    def test_human(self):
        author = author_from_github(
            {"login": "octocat", "type": "User"},
            association="COLLABORATOR",
            performed_via_github_app_id=None,
            identity=IDENTITY,
        )

        assert author.login == "octocat"
        assert author.association == "collaborator"
        assert author.is_self is False

    def test_own_app_by_performed_via_id(self):
        author = author_from_github(
            {"login": "anything[bot]", "type": "Bot"},
            association="NONE",
            performed_via_github_app_id=APP_ID,
            identity=IDENTITY,
        )

        assert author.is_self is True

    def test_own_app_by_derived_bot_login(self):
        author = author_from_github(
            {"login": "stateway-issue-agent[bot]", "type": "Bot"},
            association=None,
            performed_via_github_app_id=None,
            identity=IDENTITY,
        )

        assert author.is_self is True
        assert author.association == "unknown"

    def test_explicit_bot_login_override(self):
        identity = BotIdentity(
            app_id=APP_ID, app_name="issue-agent", app_bot_login="Custom[bot]"
        )
        author = author_from_github(
            {"login": "custom[bot]", "type": "Bot"},
            association=None,
            performed_via_github_app_id=None,
            identity=identity,
        )

        assert author.is_self is True

    def test_missing_user(self):
        author = author_from_github(
            None,
            association=None,
            performed_via_github_app_id=None,
            identity=IDENTITY,
        )

        assert author.login == ""
        assert author.is_self is False


class TestCommentDictIsOwnApp:
    def test_app_id_as_string_matches_int_payload(self):
        assert comment_dict_is_own_app(
            login=None,
            user_type="Bot",
            performed_via_github_app_id=APP_ID,
            app_id=str(APP_ID),  # type: ignore[arg-type]
            app_name="x",
        )

    def test_unparseable_app_id_never_matches_by_id(self):
        assert not comment_dict_is_own_app(
            login="someone",
            user_type="User",
            performed_via_github_app_id="garbage",
            app_id=APP_ID,
            app_name="x",
        )
