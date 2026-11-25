"""``GiteaForgeClient`` over a mocked API, with bodies from the real fixtures
(an API issue/comment/repo has the same shape as the webhook's)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from issue_agent.providers.gitea.api import GiteaAPIError
from issue_agent.providers.gitea.client import GiteaForgeClient
from tests.unit.providers.gitea.mock_api import P, Recorder, make_api

FIXTURES = Path(__file__).parent / "fixtures"
REPO = "team/app"
BOT = "issue-agent"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


ISSUE = _fixture("issues_opened")["issue"]
COMMENT = _fixture("issue_comment_created")["comment"]
BOT_COMMENT = _fixture("issue_comment_own_bot")["comment"]
REPOSITORY = _fixture("issues_opened")["repository"]


def _client(rec: Recorder, **kw: Any) -> GiteaForgeClient:
    return GiteaForgeClient(make_api(rec), REPO, bot_login=BOT, **kw)


class TestIssueState:
    @pytest.mark.parametrize(
        ("state", "expected"), [("open", "open"), ("closed", "closed"), ("weird", "open")]
    )
    def test_maps_the_state(self, state: str, expected: str):
        rec = Recorder({("GET", f"{P}/repos/{REPO}/issues/7"): {**ISSUE, "state": state}})

        assert _client(rec).get_issue_state(7) == expected

    def test_not_found_raises(self):
        with pytest.raises(GiteaAPIError):
            _client(Recorder({})).get_issue_state(7)


COMMENTS = f"{P}/repos/{REPO}/issues/1/comments"


class TestComments:
    def test_lists_the_most_recent_oldest_first(self):
        older = {**COMMENT, "id": 1, "body": "first"}
        middle = {**COMMENT, "id": 2, "body": "second"}
        rec = Recorder({("GET", COMMENTS): [older, middle, BOT_COMMENT]})

        comments = _client(rec).list_comments(1, limit=2)

        assert [c.body for c in comments] == ["second", BOT_COMMENT["body"]]
        assert [c.author.is_self for c in comments] == [False, True]
        assert comments[0].author.association == "unknown"
        assert comments[0].author.login == COMMENT["user"]["login"]
        assert comments[1].id == str(BOT_COMMENT["id"])

    def test_sends_no_paging_params(self):
        rec = Recorder({("GET", COMMENTS): [COMMENT]})

        _client(rec).list_comments(1, limit=5)

        assert not rec.requests[0].url.params

    def test_zero_limit_skips_the_call(self):
        rec = Recorder({})

        assert _client(rec).list_comments(1, limit=0) == []
        assert rec.requests == []

    def test_tolerates_junk_entries_and_empty_body(self):
        rec = Recorder({("GET", COMMENTS): ["junk", COMMENT]})
        assert len(_client(rec).list_comments(1, limit=5)) == 1

        rec = Recorder({("GET", COMMENTS): httpx.Response(200)})
        assert _client(rec).list_comments(1, limit=5) == []

    def test_post_returns_the_url(self):
        rec = Recorder({("POST", COMMENTS): httpx.Response(201, json=COMMENT)})

        assert _client(rec).post_comment(1, "hello") == COMMENT["html_url"]
        assert json.loads(rec.requests[0].content) == {"body": "hello"}

    def test_post_without_url_returns_empty(self):
        rec = Recorder({("POST", COMMENTS): httpx.Response(201, json={"id": 1})})

        assert _client(rec).post_comment(1, "hello") == ""


def test_default_branch():
    rec = Recorder({("GET", f"{P}/repos/{REPO}"): {**REPOSITORY, "default_branch": "trunk"}})

    assert _client(rec).get_default_branch() == "trunk"


ORG = f"{P}/orgs/team/labels"
REPO_LABELS = f"{P}/repos/{REPO}/labels"
ISSUE_LABELS = f"{P}/repos/{REPO}/issues/1/labels"


class TestAddLabels:
    def test_posts_ids_from_repo_and_org_labels(self):
        rec = Recorder(
            {
                ("GET", ORG): [{"id": 1, "name": "agent-generated"}],
                ("GET", REPO_LABELS): [{"id": 10, "name": "bug"}],
                ("POST", ISSUE_LABELS): [],
            }
        )

        _client(rec).add_labels(1, ["agent-generated", "bug"])

        assert json.loads(rec.calls("POST", ISSUE_LABELS)[0].content) == {"labels": [1, 10]}

    def test_unknown_label_is_refetched_once_then_skipped_never_created(self):
        rec = Recorder(
            {
                ("GET", ORG): [],
                ("GET", REPO_LABELS): [{"id": 10, "name": "bug"}],
                ("POST", ISSUE_LABELS): [],
            }
        )

        with capture_logs() as logs:
            _client(rec).add_labels(1, ["bug", "nope"])

        assert len(rec.calls("GET", REPO_LABELS)) == 2
        assert json.loads(rec.calls("POST", ISSUE_LABELS)[0].content) == {"labels": [10]}
        assert not rec.calls("POST", REPO_LABELS)
        assert [e["label"] for e in logs if e["event"] == "gitea_label_unknown"] == ["nope"]

    def test_label_created_since_the_cache_is_found_on_refetch(self):
        rec = Recorder(
            {
                ("GET", ORG): [],
                ("GET", REPO_LABELS): [
                    httpx.Response(200, json=[]),
                    httpx.Response(200, json=[{"id": 5, "name": "new"}]),
                ],
                ("POST", ISSUE_LABELS): [],
            }
        )

        _client(rec).add_labels(1, ["new"])

        assert json.loads(rec.calls("POST", ISSUE_LABELS)[0].content) == {"labels": [5]}

    def test_known_labels_use_the_cache(self):
        rec = Recorder(
            {
                ("GET", ORG): [],
                ("GET", REPO_LABELS): [{"id": 10, "name": "bug"}],
                ("POST", ISSUE_LABELS): [],
            }
        )
        api = make_api(rec)

        for _ in range(2):
            GiteaForgeClient(api, REPO, bot_login=BOT).add_labels(1, ["bug"])

        assert len(rec.calls("GET", REPO_LABELS)) == 1

    def test_nothing_known_skips_the_post(self):
        rec = Recorder({("GET", ORG): [], ("GET", REPO_LABELS): []})

        _client(rec).add_labels(1, ["nope"])

        assert not rec.calls("POST", ISSUE_LABELS)


PERM = f"{P}/repos/{REPO}/collaborators/alice/permission"


class TestAuthorPermission:
    @pytest.mark.parametrize(
        ("permission", "association"),
        [
            ("owner", "owner"),
            ("admin", "collaborator"),
            ("write", "collaborator"),
            ("read", "none"),
            ("none", "none"),
            ("something-new", "unknown"),
        ],
    )
    def test_api_mode_maps_the_permission(self, permission: str, association: str):
        rec = Recorder({("GET", PERM): {"permission": permission}})

        assert _client(rec, author_permission="api").author_permission("alice") == association

    def test_forbidden_is_unknown(self):
        rec = Recorder({("GET", PERM): httpx.Response(403)})

        assert _client(rec, author_permission="api").author_permission("alice") == "unknown"

    def test_allowlist_mode_never_asks(self):
        rec = Recorder({})

        assert _client(rec).author_permission("alice") == "unknown"
        assert rec.requests == []

    def test_empty_login_never_asks(self):
        rec = Recorder({})

        assert _client(rec, author_permission="api").author_permission("") == "unknown"
        assert rec.requests == []

    def test_other_errors_propagate(self):
        rec = Recorder({("GET", PERM): httpx.Response(500)})

        with pytest.raises(GiteaAPIError):
            _client(rec, author_permission="api").author_permission("alice")

    def test_cache_is_shared_across_clients(self):
        rec = Recorder({("GET", PERM): {"permission": "write"}})
        api = make_api(rec)

        for _ in range(3):
            GiteaForgeClient(api, REPO, bot_login=BOT, author_permission="api").author_permission(
                "alice"
            )

        assert len(rec.requests) == 1
