"""Gitea shape -> canonical model, on real fixtures and on edge cases."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from issue_agent.providers.gitea.normalize import (
    author_from_gitea,
    comment_from_gitea,
    is_pull,
    issue_ref_from_gitea,
    issue_state,
    label_names,
)

FIXTURES = Path(__file__).parent / "fixtures"
BOT = "issue-agent"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_bytes())


class TestAuthor:
    def test_association_is_always_unknown(self):
        author = author_from_gitea({"login": "maintainer", "is_admin": True}, bot_login=BOT)

        assert author.login == "maintainer"
        assert author.association == "unknown"
        assert author.is_self is False

    @pytest.mark.parametrize("login", ["issue-agent", "Issue-Agent", "ISSUE-AGENT"])
    def test_own_bot_matches_case_insensitively(self, login: str):
        assert author_from_gitea({"login": login}, bot_login=BOT).is_self is True

    def test_unset_bot_login_matches_nobody(self):
        assert author_from_gitea({"login": ""}, bot_login="").is_self is False
        assert author_from_gitea({"login": "issue-agent"}, bot_login="").is_self is False

    def test_user_type_bot_is_not_relied_upon(self):
        author = author_from_gitea({"login": "renovate", "type": "Bot"}, bot_login=BOT)

        assert author.is_self is False

    @pytest.mark.parametrize("user", [None, {}, "maintainer", {"login": 7}])
    def test_missing_or_malformed_user(self, user: Any):
        author = author_from_gitea(user, bot_login=BOT)

        assert author.login == ""
        assert author.is_self is False


class TestIssueRef:
    def test_from_a_real_payload(self):
        payload = _fixture("issues_label_updated_with_trigger")

        ref = issue_ref_from_gitea(
            payload["issue"], repo="team/app", instance_id="gitea", bot_login=BOT
        )

        assert ref.provider_instance == "gitea"
        assert ref.repo == "team/app"
        assert ref.number == payload["issue"]["number"]
        assert ref.global_id == str(payload["issue"]["id"])
        assert ref.title == "Crash when the config file is empty"
        assert ref.labels == ["agent:fix", "wontfix"]
        assert ref.author.login == "maintainer"
        assert ref.author.association == "unknown"
        assert ref.url == payload["issue"]["html_url"]
        assert ref.state == "open"

    def test_labels_override(self):
        payload = _fixture("issues_label_cleared")

        ref = issue_ref_from_gitea(
            payload["issue"], repo="team/app", instance_id="gitea", bot_login=BOT, labels=[]
        )

        assert payload["issue"]["labels"]  # Gitea still sends the old set
        assert ref.labels == []

    def test_minimal_issue(self):
        ref = issue_ref_from_gitea(
            {"number": 3, "title": None, "body": None, "labels": None, "user": None},
            repo="team/app",
            instance_id="gitea",
            bot_login=BOT,
        )

        assert (ref.number, ref.title, ref.body, ref.labels) == (3, "", "", [])
        assert ref.global_id is None
        assert ref.state is None

    def test_missing_number_is_zero(self):
        ref = issue_ref_from_gitea({}, repo="team/app", instance_id="gitea", bot_login=BOT)

        assert ref.number == 0


class TestLabels:
    def test_skips_malformed_entries(self):
        issue = {"labels": [{"name": "bug"}, {"name": 3}, "wontfix", {}, {"name": "agent:fix"}]}

        assert label_names(issue) == ["bug", "agent:fix"]

    def test_not_a_list(self):
        assert label_names({"labels": {"name": "bug"}}) == []


class TestComment:
    def test_from_a_real_payload(self):
        comment = comment_from_gitea(_fixture("issue_comment_created")["comment"], bot_login=BOT)

        assert comment.body == "/agent fix please"
        assert comment.author.login == "maintainer"
        assert comment.author.is_self is False
        assert comment.created_at is not None
        assert comment.created_at.tzinfo is not None

    def test_own_bot_comment(self):
        comment = comment_from_gitea(_fixture("issue_comment_own_bot")["comment"], bot_login=BOT)

        assert comment.author.is_self is True

    def test_minimal_comment(self):
        comment = comment_from_gitea({"id": 9, "created_at": ""}, bot_login=BOT)

        assert comment.id == "9"
        assert comment.body == ""
        assert comment.created_at is None

    def test_parses_the_timestamp(self):
        comment = comment_from_gitea({"id": 1, "created_at": "2026-11-25T18:44:26Z"}, bot_login=BOT)

        assert comment.created_at == datetime(2026, 11, 25, 18, 44, 26, tzinfo=UTC)

    def test_malformed_timestamp_is_a_validation_error(self):
        with pytest.raises(ValidationError):
            comment_from_gitea({"id": 1, "created_at": "yesterday"}, bot_login=BOT)


class TestIsPull:
    def test_real_payloads(self):
        assert is_pull(_fixture("issue_comment_on_pull_request")) is True
        assert is_pull(_fixture("issue_comment_created")) is False
        assert is_pull(_fixture("issues_opened")) is False

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"is_pull": True}, True),
            ({"is_pull": False, "issue": {"pull_request": {"merged": False}}}, True),
            ({"issue": {"pull_request": None}}, False),
            ({"is_pull": "yes"}, False),
            ({"issue": "x"}, False),
        ],
    )
    def test_either_signal(self, payload: dict[str, Any], expected: bool):
        assert is_pull(payload) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("open", "open"), ("CLOSED", "closed"), ("merged", None), (None, None), (1, None)],
)
def test_issue_state(raw: Any, expected: str | None):
    assert issue_state(raw) == expected
