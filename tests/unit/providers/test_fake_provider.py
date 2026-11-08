"""The fake forge's outbound state — what core tests seed and inspect."""

from __future__ import annotations

from issue_agent.core.models import PRRef
from tests.fakes.fake_provider import FakeForge, FakeProvider, FakeProviderWithPulls

REPO = "org/repo"


def test_posted_comments_read_back_as_the_bots_own():
    plugin = FakeProvider("fake", secret="s")
    forge = plugin.client(REPO)

    url = forge.post_comment(7, "hello")

    assert url.startswith("https://forge.example/org/repo/")
    assert plugin.forge.posted(REPO, 7) == ["hello"]
    [comment] = forge.list_comments(7, 10)
    assert comment.author.is_self is True


def test_seeded_state_is_shared_by_every_client():
    shared = FakeForge(
        issue_states={(REPO, 7): "closed"},
        default_branches={REPO: "develop"},
        permissions={"maintainer": "member"},
    )
    plugin = FakeProvider("fake", secret="s", forge=shared)

    assert plugin.client(REPO).get_issue_state(7) == "closed"
    assert plugin.client(REPO).get_default_branch() == "develop"
    assert plugin.client(REPO).author_permission("maintainer") == "member"
    assert plugin.client(REPO).author_permission("stranger") == "unknown"


def test_git_auth_env_mints_a_fresh_token_per_call():
    plugin = FakeProvider("fake", secret="s")

    plugin.transport().git_auth_env(REPO)
    plugin.transport().git_auth_env(REPO)

    assert plugin.forge.tokens == ["fake-token-1", "fake-token-2"]


def test_merge_requests_round_trip():
    plugin = FakeProviderWithPulls("fake", secret="s")
    pulls = plugin.pulls(REPO)

    assert pulls.find_open_pr("agent/issue-7") is None
    pr = pulls.create_pr(
        head="agent/issue-7", base="main", title="t", body="b", draft=False, labels=["x"]
    )

    assert pulls.find_open_pr("agent/issue-7") == pr
    assert pr == PRRef(
        number=100,
        url="https://forge.example/org/repo/-/merge_requests/100",
        head_ref="agent/issue-7",
    )
    assert plugin.forge.labels[(REPO, 100)] == ["x"]
    pulls.comment_on_pr(pr, "updated")
    assert plugin.forge.posted(REPO, 100) == ["updated"]
    assert pulls.pr_has_conflicts(pr, "sha") is None


def test_merge_request_state_defaults_to_open_and_follows_the_forge():
    from datetime import UTC, datetime

    from issue_agent.core.models import PRState

    plugin = FakeProviderWithPulls("fake", secret="s")
    pulls = plugin.pulls(REPO)
    pr = pulls.create_pr(
        head="agent/issue-8", base="main", title="t", body="b", draft=False, labels=[]
    )

    assert pulls.get_pr_state(pr.number) == PRState(state="open", closed_at=None)

    merged = PRState(state="merged", closed_at=datetime(2026, 10, 1, tzinfo=UTC))
    plugin.forge.pr_states[(REPO, pr.number)] = merged
    assert pulls.get_pr_state(pr.number) == merged
