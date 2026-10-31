from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.policy.guards import (
    check_author_allowed,
    check_comment_author_allowed,
    check_daily_budget,
    check_repo_allowed,
    validate_task_payload,
)
from issue_agent.queue.models import Task, TriggerKind
from issue_agent.queue.store import SQLiteStore
from tests.helpers.factories import (
    make_author,
    make_comment,
    make_issue,
    make_task,
    make_trigger_event,
)
from tests.helpers.settings import github_providers


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        yaml_file=None,
    )


def _make_settings(**overrides: dict) -> Settings:
    repo_routes = overrides.pop(
        "REPO_ROUTES",
        [
            {
                "issues": "stateway-io/stateway-issues",
                "target": "stateway-io/stateway-agent-fixtures",
            }
        ],
    )

    defaults = dict(
        PROVIDERS=github_providers(app_id="1", app_private_key="k", webhook_secret="s"),
        LLM_API_KEY="k",
        DATA_DIR="/tmp/test-guards-data",
        REPO_ROUTES=repo_routes,
    )
    defaults.update(overrides)
    return SettingsNoEnv(**defaults)


def _make_task(actor: str = "testuser", **overrides: Any) -> Task:
    trigger = make_trigger_event(
        issue=make_issue(number=42, title="test", labels=[]),
        actor=make_author(login=actor),
    )
    return make_task(trigger, **overrides)


# ── validate_task_payload ────────────────────────────────────


class TestValidateTaskPayload:
    def test_valid_trigger(self) -> None:
        ok, msg = validate_task_payload(_make_task())
        assert ok
        assert msg == ""

    def test_invalid_trigger_json(self) -> None:
        task = _make_task(trigger_json="{{{{invalid")
        ok, msg = validate_task_payload(task)
        assert not ok
        assert msg == "invalid_trigger"

    def test_missing_trigger(self) -> None:
        # A legacy row the backfill couldn't re-normalize.
        task = _make_task(trigger_json=None)
        ok, msg = validate_task_payload(task)
        assert not ok
        assert msg == "invalid_trigger"

    def test_trigger_of_the_wrong_shape(self) -> None:
        task = _make_task(trigger_json='{"issue": {"number": 42}}')
        ok, msg = validate_task_payload(task)
        assert not ok
        assert msg == "invalid_trigger"

    def test_issue_number_zero(self) -> None:
        trigger = make_trigger_event(issue=make_issue(number=0))
        ok, msg = validate_task_payload(make_task(trigger))
        assert not ok
        assert msg == "trigger_missing_issue_number"

    def test_missing_repo(self) -> None:
        trigger = make_trigger_event(issue=make_issue(repo=""))
        ok, msg = validate_task_payload(make_task(trigger))
        assert not ok
        assert msg == "trigger_missing_repo"

    def test_ignores_legacy_payload_json(self) -> None:
        # payload_json is only kept for the backfill; it's never read here.
        task = _make_task(payload_json="{{{{invalid")
        ok, _ = validate_task_payload(task)
        assert ok


# ── check_repo_allowed ───────────────────────────────────────


class TestCheckRepoAllowed:
    def test_repo_allowed(self) -> None:
        settings = _make_settings()
        task = _make_task()
        ok, msg = check_repo_allowed(task, settings)
        assert ok
        assert msg == ""

    def test_repo_not_allowed(self) -> None:
        settings = _make_settings(
            REPO_ROUTES=[{"issues": "stateway-io/other-repo"}]
        )
        task = _make_task()
        ok, msg = check_repo_allowed(task, settings)
        assert not ok
        assert "repo_not_allowed" in msg
        assert "stateway-io/stateway-issues" in msg

    def test_multiple_allowed_repos(self) -> None:
        settings = _make_settings(
            REPO_ROUTES=[{"issues": "org/a"}, {"issues": "org/b"}, {"issues": "org/c"}]
        )
        task = _make_task(repo="org/b")
        ok, msg = check_repo_allowed(task, settings)
        assert ok


# ── check_author_allowed ─────────────────────────────────────


class TestCheckAuthorAllowed:
    def test_no_allowlist_allows_any(self) -> None:
        settings = _make_settings()
        task = _make_task()
        ok, msg = check_author_allowed(task, settings)
        assert ok
        assert msg == ""

    def test_author_in_allowlist(self) -> None:
        settings = _make_settings(ALLOWED_AUTHORS="testuser,admin")
        task = _make_task()
        ok, msg = check_author_allowed(task, settings)
        assert ok

    def test_author_not_in_allowlist(self) -> None:
        settings = _make_settings(ALLOWED_AUTHORS="admin,bot")
        task = _make_task()
        ok, msg = check_author_allowed(task, settings)
        assert not ok
        assert "author_not_allowed" in msg
        assert "testuser" in msg

    def test_missing_actor(self) -> None:
        settings = _make_settings(ALLOWED_AUTHORS="testuser")
        task = _make_task(actor="")
        ok, msg = check_author_allowed(task, settings)
        assert not ok
        assert msg == "trigger_missing_actor"

    def test_invalid_trigger_in_author_check(self) -> None:
        settings = _make_settings(ALLOWED_AUTHORS="testuser")
        task = _make_task(trigger_json="not json")
        ok, msg = check_author_allowed(task, settings)
        assert not ok
        assert msg == "invalid_trigger"

    def test_actor_is_checked_not_issue_author(self) -> None:
        # On a `labeled` event the actor is whoever added the label.
        settings = _make_settings(ALLOWED_AUTHORS="triager")
        trigger = make_trigger_event(
            TriggerKind.LABEL_ADDED,
            issue=make_issue(author=make_author(login="outsider")),
            actor=make_author(login="triager"),
        )
        ok, _ = check_author_allowed(make_task(trigger), settings)
        assert ok

    def test_author_whitespace_trimmed(self) -> None:
        settings = _make_settings(ALLOWED_AUTHORS="  user1 , user2  ")
        task = _make_task(actor="user2")
        ok, msg = check_author_allowed(task, settings)
        assert ok


# ── check_comment_author_allowed ─────────────────────────────


def _comment_task(login: str, association: str) -> Task:
    trigger = make_trigger_event(
        TriggerKind.COMMENT_COMMAND,
        issue=make_issue(author=make_author(login="reporter")),
        trigger_comment=make_comment(
            author=make_author(login=login, association=association)
        ),
    )
    return make_task(trigger, trigger_kind=TriggerKind.COMMENT_COMMAND)


def _unknown(login: str) -> str:
    return "unknown"


class TestCheckCommentAuthorAllowed:
    """The deferred half of the ingestion filter (PRD §3.1)."""

    def test_unknown_third_party_is_rejected(self) -> None:
        ok, msg = check_comment_author_allowed(
            _comment_task("stranger", "unknown"), _make_settings(), _unknown
        )
        assert not ok
        assert msg == "comment_author_not_allowed"

    def test_unknown_issue_author_passes(self) -> None:
        ok, _ = check_comment_author_allowed(
            _comment_task("reporter", "unknown"), _make_settings(), _unknown
        )
        assert ok

    def test_unknown_allowlisted_login_passes(self) -> None:
        settings = _make_settings(COMMENTS_AUTHOR_FILTER_ALLOWLIST=["stranger"])
        ok, _ = check_comment_author_allowed(
            _comment_task("stranger", "unknown"), settings, _unknown
        )
        assert ok

    def test_association_resolved_by_the_forge_is_judged(self) -> None:
        resolved: list[str] = []

        def permission(login: str) -> str:
            resolved.append(login)
            return "collaborator"

        ok, _ = check_comment_author_allowed(
            _comment_task("maintainer", "unknown"), _make_settings(), permission
        )

        assert ok
        assert resolved == ["maintainer"]

    def test_resolved_disallowed_association_is_rejected(self) -> None:
        ok, msg = check_comment_author_allowed(
            _comment_task("stranger", "unknown"), _make_settings(), lambda _: "none"
        )
        assert not ok
        assert msg == "comment_author_not_allowed"

    def test_failed_lookup_fails_closed(self) -> None:
        def permission(login: str) -> str:
            raise RuntimeError("forge down")

        ok, _ = check_comment_author_allowed(
            _comment_task("stranger", "unknown"), _make_settings(), permission
        )
        assert not ok

    def test_known_association_was_already_judged_at_ingestion(self) -> None:
        # A known disallowed association never reaches the worker; one that
        # did (e.g. config changed since) isn't second-guessed here.
        lookup = MagicMock()
        ok, _ = check_comment_author_allowed(
            _comment_task("stranger", "none"), _make_settings(), lookup
        )
        assert ok
        lookup.assert_not_called()

    def test_non_comment_trigger_passes(self) -> None:
        ok, _ = check_comment_author_allowed(_make_task(), _make_settings(), _unknown)
        assert ok


# ── check_daily_budget ───────────────────────────────────────


_SCOPE = "owner/repo-a"


class TestCheckDailyBudget:
    def test_budget_under_limit(self) -> None:
        store = SQLiteStore(":memory:")
        store.init()
        settings = _make_settings(MAX_DAILY_BUDGET_USD=50.0)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        store.add_daily_budget(today, _SCOPE, 10.0)

        ok, msg = check_daily_budget(store, settings, scope=_SCOPE)
        assert ok
        assert msg == ""

    def test_budget_at_limit(self) -> None:
        store = SQLiteStore(":memory:")
        store.init()
        settings = _make_settings(MAX_DAILY_BUDGET_USD=50.0)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        store.add_daily_budget(today, _SCOPE, 50.0)

        ok, msg = check_daily_budget(store, settings, scope=_SCOPE)
        assert not ok
        assert "daily_budget_exceeded" in msg

    def test_budget_over_limit(self) -> None:
        store = SQLiteStore(":memory:")
        store.init()
        settings = _make_settings(MAX_DAILY_BUDGET_USD=50.0)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        store.add_daily_budget(today, _SCOPE, 75.0)

        ok, msg = check_daily_budget(store, settings, scope=_SCOPE)
        assert not ok
        assert "daily_budget_exceeded" in msg

    def test_no_spend_today(self) -> None:
        store = SQLiteStore(":memory:")
        store.init()
        settings = _make_settings(MAX_DAILY_BUDGET_USD=50.0)

        ok, msg = check_daily_budget(store, settings, scope=_SCOPE)
        assert ok
        assert msg == ""

    def test_budget_zero_limit(self) -> None:
        store = SQLiteStore(":memory:")
        store.init()
        settings = _make_settings(MAX_DAILY_BUDGET_USD=0.0)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        store.add_daily_budget(today, _SCOPE, 0.0)

        ok, msg = check_daily_budget(store, settings, scope=_SCOPE)
        assert not ok
        assert "daily_budget_exceeded" in msg

    def test_budget_multiple_days(self) -> None:
        store = SQLiteStore(":memory:")
        store.init()
        settings = _make_settings(MAX_DAILY_BUDGET_USD=50.0)
        yesterday = datetime.now(UTC).strftime("%Y-%m-%d") + "_no_match"
        store.add_daily_budget(yesterday, _SCOPE, 200.0)

        ok, msg = check_daily_budget(store, settings, scope=_SCOPE)
        assert ok

    def test_budget_scoped_by_repo(self) -> None:
        """A budget slammed into the ceiling for one repo must not block a
        different repo on the same day — each target_repo gets its own
        daily allowance. A test that reverted check_daily_budget to a
        single global scope would still pass every test above (they all
        use the same scope), but would incorrectly block repo-b here."""
        store = SQLiteStore(":memory:")
        store.init()
        settings = _make_settings(MAX_DAILY_BUDGET_USD=50.0)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        store.add_daily_budget(today, "owner/repo-a", 50.0)

        ok_a, msg_a = check_daily_budget(store, settings, scope="owner/repo-a")
        assert not ok_a
        assert "daily_budget_exceeded" in msg_a

        ok_b, msg_b = check_daily_budget(store, settings, scope="owner/repo-b")
        assert ok_b
        assert msg_b == ""
