"""``GiteaPullRequestClient`` over a mocked API, with PR bodies captured
from Gitea 28.4.0 (``fixtures/api/pull_*.json``)."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from issue_agent.core.models import PRRef
from issue_agent.providers.gitea.api import GiteaNotFoundError
from issue_agent.providers.gitea.pr import GiteaPullRequestClient
from tests.unit.providers.gitea.mock_api import P, Recorder, make_api

API_FIXTURES = Path(__file__).parent / "fixtures" / "api"
REPO = "team/app"
PULLS = f"{P}/repos/{REPO}/pulls"
ORG = f"{P}/orgs/team/labels"
REPO_LABELS = f"{P}/repos/{REPO}/labels"


def _pull(name: str = "open", **overrides: Any) -> dict[str, Any]:
    pr = json.loads((API_FIXTURES / f"pull_{name}.json").read_text())
    for key, value in overrides.items():
        pr[key] = value
    return pr


def _with_head(pr: dict[str, Any], **head: Any) -> dict[str, Any]:
    pr = copy.deepcopy(pr)
    pr["head"].update(head)
    return pr


OPEN = _pull("open")
SHA = OPEN["head"]["sha"]


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _pulls(rec: Recorder, sleep: Sleeps | None = None, **kw: Any) -> GiteaPullRequestClient:
    return GiteaPullRequestClient(
        make_api(rec), REPO, label_agent_generated="agent-generated", sleep=sleep or Sleeps(), **kw
    )


def _ref(pr: dict[str, Any] = OPEN) -> PRRef:
    return PRRef(number=pr["number"], url=pr["html_url"], head_ref=pr["head"]["ref"])


class TestFindOpenPr:
    def test_finds_the_same_repo_branch(self):
        rec = Recorder({("GET", PULLS): [_pull("open")]})

        found = _pulls(rec).find_open_pr(OPEN["head"]["ref"])

        assert found == _ref()
        assert dict(rec.requests[0].url.params)["state"] == "open"

    def test_none_for_another_branch(self):
        rec = Recorder({("GET", PULLS): [OPEN]})

        assert _pulls(rec).find_open_pr("agent/issue-99-x") is None

    def test_a_forks_branch_of_the_same_name_is_not_ours(self):
        fork = _with_head(OPEN, repo={**OPEN["head"]["repo"], "full_name": "someone/app"})
        rec = Recorder({("GET", PULLS): [fork]})

        assert _pulls(rec).find_open_pr(OPEN["head"]["ref"]) is None

    def test_deleted_fork_does_not_match(self):
        rec = Recorder({("GET", PULLS): [_with_head(OPEN, repo=None), "junk"]})

        assert _pulls(rec).find_open_pr(OPEN["head"]["ref"]) is None

    def test_repo_name_case_is_ignored(self):
        upper = _with_head(OPEN, repo={**OPEN["head"]["repo"], "full_name": "Team/App"})
        rec = Recorder({("GET", PULLS): [upper]})

        assert _pulls(rec).find_open_pr(OPEN["head"]["ref"]) == _ref()

    def test_walks_pages(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("issue_agent.providers.gitea.api.PAGE_SIZE", 1)
        other = _with_head(OPEN, ref="other")
        rec = Recorder(
            {
                ("GET", PULLS): [
                    httpx.Response(200, json=[other]),
                    httpx.Response(200, json=[OPEN]),
                    httpx.Response(200, json=[]),
                ]
            }
        )

        assert _pulls(rec).find_open_pr(OPEN["head"]["ref"]) == _ref()


class TestCreatePr:
    def _rec(self) -> Recorder:
        return Recorder(
            {
                ("GET", ORG): [{"id": 1, "name": "agent-generated"}],
                ("GET", REPO_LABELS): [{"id": 10, "name": "bug"}],
                ("POST", PULLS): httpx.Response(201, json=OPEN),
            }
        )

    def test_posts_the_title_as_given_with_label_ids(self):
        rec = self._rec()

        ref = _pulls(rec).create_pr(
            head="agent/issue-1-fix",
            base="main",
            title="WIP: fix it",
            body="Closes #1",
            draft=False,
            labels=["bug"],
        )

        assert ref == _ref()
        assert json.loads(rec.calls("POST", PULLS)[0].content) == {
            "head": "agent/issue-1-fix",
            "base": "main",
            "title": "WIP: fix it",
            "body": "Closes #1",
            "labels": [1, 10],
        }

    def test_draft_flag_changes_nothing(self):
        rec = self._rec()

        _pulls(rec).create_pr(
            head="h", base="main", title="t", body="b", draft=True, labels=["nope"]
        )

        sent = json.loads(rec.calls("POST", PULLS)[0].content)
        assert sent["title"] == "t"
        assert "draft" not in sent
        assert sent["labels"] == [1]


def test_comment_on_pr_uses_the_issue_comments_endpoint():
    comments = f"{P}/repos/{REPO}/issues/{OPEN['number']}/comments"
    rec = Recorder({("POST", comments): httpx.Response(201, json={"html_url": "u"})})

    assert _pulls(rec).comment_on_pr(_ref(), "hi") == "u"
    assert json.loads(rec.requests[0].content) == {"body": "hi"}


def test_comment_on_pr_without_url():
    comments = f"{P}/repos/{REPO}/issues/{OPEN['number']}/comments"
    rec = Recorder({("POST", comments): httpx.Response(201, json={})})

    assert _pulls(rec).comment_on_pr(_ref(), "hi") == ""


PULL = f"{PULLS}/{OPEN['number']}"


def _reads(*mergeable: Any, sha: str = SHA) -> list[httpx.Response]:
    return [
        httpx.Response(200, json=_with_head(OPEN, sha=sha) | {"mergeable": m}) for m in mergeable
    ]


class TestPrHasConflicts:
    @pytest.mark.parametrize(
        ("reads", "expected"),
        [
            ((True, True, True), False),
            ((False, False, False), True),
            # The measured transients: false on a fresh clean PR, a stale
            # true right after a conflicting push.
            ((False, True, True, True), False),
            ((True, False, False, False), True),
            ((True, None, True, True, True), False),
        ],
    )
    def test_verdict_needs_consecutive_agreeing_reads(self, reads: tuple, expected: bool):
        rec = Recorder({("GET", PULL): _reads(*reads)})
        sleeps = Sleeps()

        assert _pulls(rec, sleeps).pr_has_conflicts(_ref(), SHA) is expected
        assert len(rec.requests) == len(reads)
        assert sleeps.calls == [1.0] * (len(reads) - 1)

    def test_reads_of_an_older_head_do_not_count(self):
        responses = _reads(False, False, sha="0" * 40) + _reads(False, False, False)
        rec = Recorder({("GET", PULL): responses})

        assert _pulls(rec).pr_has_conflicts(_ref(), SHA) is True
        assert len(rec.requests) == 5

    def test_no_agreement_within_the_cap_is_none(self):
        rec = Recorder({("GET", PULL): _reads(*[True, False] * 3)})
        sleeps = Sleeps()

        assert _pulls(rec, sleeps, conflict_max_reads=6).pr_has_conflicts(_ref(), SHA) is None
        assert len(rec.requests) == 6
        assert len(sleeps.calls) == 5

    def test_head_never_caught_up_is_none(self):
        rec = Recorder({("GET", PULL): _reads(True, sha="0" * 40)})

        assert _pulls(rec, conflict_max_reads=4).pr_has_conflicts(_ref(), SHA) is None
        assert len(rec.requests) == 4

    def test_api_error_is_none(self):
        rec = Recorder({("GET", PULL): [*_reads(False), httpx.Response(500)]})

        assert _pulls(rec).pr_has_conflicts(_ref(), SHA) is None

    def test_non_object_body_does_not_count(self):
        rec = Recorder({("GET", PULL): [httpx.Response(200, json=[]), *_reads(True, True, True)]})

        assert _pulls(rec).pr_has_conflicts(_ref(), SHA) is False

    def test_knobs(self):
        rec = Recorder({("GET", PULL): _reads(False)})
        sleeps = Sleeps()
        pulls = _pulls(rec, sleeps, conflict_reads=1, conflict_interval_seconds=0.5)

        assert pulls.pr_has_conflicts(_ref(), SHA) is True
        assert sleeps.calls == []


class TestGetPrState:
    def _state(self, pr: dict[str, Any]):
        rec = Recorder({("GET", f"{PULLS}/{pr['number']}"): pr})
        return _pulls(rec).get_pr_state(pr["number"])

    def test_open(self):
        state = self._state(_pull("open"))

        assert (state.state, state.closed_at) == ("open", None)

    def test_merged_at_merged_at(self):
        pr = _pull("merged")
        state = self._state(pr)

        assert state.state == "merged"
        assert state.closed_at == datetime.fromisoformat(pr["merged_at"])
        assert state.closed_at.tzinfo is not None

    def test_closed_at_closed_at(self):
        state = self._state(_pull("closed"))

        assert state.state == "closed"
        assert state.closed_at is not None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-11-25T23:57:50Z", datetime(2026, 11, 25, 23, 57, 50, tzinfo=UTC)),
            ("2026-11-26T01:57:50+02:00", datetime(2026, 11, 25, 23, 57, 50, tzinfo=UTC)),
            ("2026-11-25T23:57:50", datetime(2026, 11, 25, 23, 57, 50, tzinfo=UTC)),
            (None, None),
            ("", None),
            ("yesterday", None),
        ],
    )
    def test_timestamps_in_utc(self, raw: Any, expected: datetime | None):
        state = self._state(_pull("closed", closed_at=raw))

        assert state.closed_at == expected
        if expected is not None:
            assert state.closed_at.utcoffset().total_seconds() == 0

    def test_unknown_pr_raises(self):
        with pytest.raises(GiteaNotFoundError):
            _pulls(Recorder({})).get_pr_state(404)
