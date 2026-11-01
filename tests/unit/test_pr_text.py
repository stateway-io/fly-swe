from __future__ import annotations

from issue_agent.core.pr_text import (
    TRUNCATION_NOTE,
    branch_name,
    commit_message,
    fit_body,
    pr_body,
    pr_title,
    slugify,
)


class TestSlugify:
    def test_basic(self) -> None:
        assert slugify("Fix typo in README") == "fix-typo-in-readme"

    def test_special_chars(self) -> None:
        assert slugify("Bug: can't parse DMN!") == "bug-can-t-parse-dmn"

    def test_truncation(self) -> None:
        long = "a" * 100
        result = slugify(long)
        assert len(result) == 30

    def test_strip_leading_trailing_hyphens(self) -> None:
        assert slugify("--hello--") == "hello"

    def test_collapse_multiple_hyphens(self) -> None:
        assert slugify("a   b---c   d") == "a-b-c-d"


class TestBranchName:
    def test_trunk_based(self) -> None:
        assert (
            branch_name(7, "Fix typo", "trunk_based", "agent", "main", slug_max_length=30)
            == "agent/issue-7-fix-typo"
        )

    def test_gitflow_uses_the_base_branch(self) -> None:
        assert (
            branch_name(7, "Fix typo", "gitflow", "agent", "feature/x", slug_max_length=30)
            == "feature/x-fix-typo"
        )

    def test_slug_max_length(self) -> None:
        name = branch_name(1, "a" * 50, "trunk_based", "agent", "main", slug_max_length=5)
        assert name == "agent/issue-1-aaaaa"


class TestRefsArePassedThrough:
    """The forge formats references; the text builders never assume `#N`."""

    def test_commit_message(self) -> None:
        assert commit_message("!12", "fix it", "bot") == ("fix(!12): fix it\n\nCo-authored-by: bot")

    def test_pr_title(self) -> None:
        assert pr_title("org/repo#3", "fix it") == "[agent] fix(org/repo#3): fix it"


class TestPRBody:
    def _body(self, closing_reference: str | None) -> str:
        return pr_body(
            issue_ref="#5",
            closing_reference=closing_reference,
            task_id="t1",
            description="desc",
            agent_name="Agent",
        )

    def test_closing_reference_heads_the_body(self) -> None:
        assert self._body("Closes #5").startswith("## Issue\nCloses #5\n\n")

    def test_without_auto_close_names_the_issue_only(self) -> None:
        body = self._body(None)

        assert body.startswith("## Issue\n#5\n\n")
        assert "Closes" not in body


class TestFitBody:
    def test_short_body_is_untouched(self) -> None:
        assert fit_body("hello", 10) == "hello"

    def test_long_body_is_cut_to_the_limit_and_says_so(self) -> None:
        body = fit_body("x" * 100, 40)
        assert len(body) == 40
        assert body.endswith(TRUNCATION_NOTE)

    def test_limit_below_the_note_cuts_without_it(self) -> None:
        assert fit_body("x" * 100, 5) == "xxxxx"
