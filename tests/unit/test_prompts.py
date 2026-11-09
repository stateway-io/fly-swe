from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from jinja2 import StrictUndefined, Template
from pydantic_settings import SettingsConfigDict

from issue_agent.config import DEFAULT_CATEGORY_PROMPT_MAP, Settings
from issue_agent.queue.models import Task, TriggerKind
from issue_agent.runner.prompts import (
    _PROMPT_DIR,
    INSTANCE_TEMPLATE,
    _read_prompt_file,
    build_branch_status,
    build_comments_block,
    build_user_message,
    format_instructions_files,
    full_suite_note,
    prompt_vars,
)
from issue_agent.runner.prompts import (
    load_diagnose_system_prompt as _load_diagnose_system_prompt,
)
from issue_agent.runner.prompts import (
    load_system_prompt as _load_system_prompt,
)
from tests.fakes.fake_provider import FakeProvider
from tests.helpers.factories import (
    make_author,
    make_comment,
    make_issue,
    make_task,
    make_trigger_event,
)
from tests.helpers.github import GITHUB_TERMS as TERMS
from tests.helpers.settings import github_providers

if TYPE_CHECKING:
    from issue_agent.core.models import Comment, IssueRef


class _Settings(Settings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8", env_prefix="", extra="ignore", yaml_file=None
    )


def _settings(**overrides) -> Settings:
    base = dict(
        PROVIDERS=github_providers(app_private_key="k", webhook_secret="s"),
        AGENT_NAME="Issue Agent",
        LLM_API_KEY="k",
    )
    base.update(overrides)
    return _Settings(**base)


def _comment_task(*, trigger_comment: Comment | None = None, issue_author="reporter") -> Task:
    trigger = make_trigger_event(
        TriggerKind.COMMENT_COMMAND,
        issue=make_issue(
            repo="o/r",
            number=42,
            title="Bug",
            body="broken",
            labels=[],
            author=make_author(login=issue_author),
        ),
        actor=make_author(login="reporter"),
        trigger_comment=trigger_comment,
    )
    return make_task(trigger, id="t-1")


def _trigger_comment(id: str, body: str, login: str = "reporter") -> Comment:
    return make_comment(
        id=id,
        author=make_author(login=login, association="none"),
        body=body,
        created_at="2026-09-08T10:00:00Z",
    )


def _history_comment(
    id: int, login: str, association: str, body: str, *, is_self: bool = False
) -> Comment:
    return make_comment(
        id=str(id),
        author=make_author(login=login, association=association, is_self=is_self),
        body=body,
        created_at=f"2026-09-08T09:{id:02d}:00Z",
    )


VOCAB = prompt_vars(TERMS)


def _with_vocab(text: str, terms=TERMS) -> str:
    """Fill in only the forge vocabulary, as build_agent's render would,
    leaving the other placeholders ({{ bot_name }}, ...) for the assertions
    that look for them."""
    for key, value in terms.model_dump().items():
        text = text.replace(f"{{{{ terms.{key} }}}}", str(value))
    return text.replace("{{ instructions_files }}", format_instructions_files(terms))


def load_system_prompt(category: str | None) -> str:
    return _with_vocab(_load_system_prompt(category, DEFAULT_CATEGORY_PROMPT_MAP))


def load_diagnose_system_prompt() -> str:
    return _with_vocab(_load_diagnose_system_prompt())


@pytest.fixture(autouse=True)
def _verify_prompts_exist() -> None:
    assert (_PROMPT_DIR / "system.md").exists(), "system.md not found"
    assert (_PROMPT_DIR / "bug_fix.md").exists(), "bug_fix.md not found"
    assert (_PROMPT_DIR / "doc_fix.md").exists(), "doc_fix.md not found"


def _make_task(
    *, actor: str = "test-user", issue: IssueRef | None = None, **issue_overrides
) -> Task:
    issue = issue or make_issue(
        number=42,
        title="Fix typo in README",
        body="There is a typo on line 5.",
        labels=["docs"],
        **issue_overrides,
    )
    return make_task(make_trigger_event(issue=issue, actor=make_author(login=actor)), id="task-001")


class TestLoadSystemPrompt:
    def test_loads_base_system_prompt(self) -> None:
        result = load_system_prompt(None)
        assert "{{ bot_name }}" in result

    def test_injects_bug_fix_instructions(self) -> None:
        result = load_system_prompt("bug")
        assert "classified as a **bug**" in result
        assert "Reproduce the bug" in result

    def test_injects_doc_fix_instructions(self) -> None:
        result = load_system_prompt("docs")
        assert "classified as **documentation**" in result
        assert "Modify ONLY documentation files" in result

    def test_empty_instructions_for_none(self) -> None:
        result = load_system_prompt(None)
        assert "{label_instructions}" not in result

    def test_empty_instructions_for_unknown_category(self) -> None:
        result = load_system_prompt("nonexistent-category")
        assert "{label_instructions}" not in result

    def test_task_context_placeholder_is_gone(self) -> None:
        # {task_context} was a single-brace placeholder that was never
        # substituted by anything — the harness's own instance_template
        # ({{ task }}, a separate and working Jinja mechanism) already
        # delivers the issue content. It must not reappear.
        for cat in ["bug", "docs", "chore", "agent_fix_please", None]:
            result = load_system_prompt(cat)
            assert "{task_context}" not in result, f"stale placeholder for {cat}"

    def test_chore_uses_bug_fix_instructions(self) -> None:
        result = load_system_prompt("chore")
        assert "classified as a **bug**" in result

    def test_agent_fix_please_uses_bug_fix_instructions(self) -> None:
        result = load_system_prompt("agent_fix_please")
        assert "classified as a **bug**" in result

    def test_no_label_instructions_placeholder_left(self) -> None:
        result = load_system_prompt("bug")
        assert "{label_instructions}" not in result

    def test_all_prompt_files_have_content(self) -> None:
        for filename in ["system.md", "bug_fix.md", "doc_fix.md"]:
            content = _read_prompt_file(filename)
            assert content.strip(), f"{filename} is empty"

    def test_triage_only_prompt_file_removed(self) -> None:
        assert not (_PROMPT_DIR / "triage_only.md").exists()

    def test_guess_success_directory_removed(self) -> None:
        assert not (_PROMPT_DIR / "guess_success").exists()

    def test_no_hardcoded_ecosystem_stack_remains(self) -> None:
        forbidden = ["Stateway", "Node.js", "npm ", "TypeScript", "BPMN", "Fastify", "Drizzle"]
        for filename in ["system.md", "bug_fix.md", "doc_fix.md"]:
            content = _read_prompt_file(filename)
            for term in forbidden:
                assert term not in content, f"{filename} still contains {term!r}"

    def test_no_fictitious_ci_label_reference(self) -> None:
        result = load_system_prompt(None)
        assert "agent-can-touch-ci" not in result

    def test_system_prompt_has_project_profile_placeholders(self) -> None:
        result = load_system_prompt(None)
        for placeholder in (
            "{{ test_command }}",
            "{{ lint_command }}",
            "{{ protected_paths }}",
            "{{ relevant_extensions }}",
        ):
            assert placeholder in result

    def test_bug_fix_and_doc_fix_have_project_profile_placeholders(self) -> None:
        bug_fix = load_system_prompt("bug")
        assert "{{ test_command }}" in bug_fix
        assert "{{ lint_command }}" in bug_fix
        assert "{{ protected_paths }}" in bug_fix

        doc_fix = load_system_prompt("docs")
        assert "{{ lint_command }}" in doc_fix
        assert "{{ relevant_extensions }}" in doc_fix

    def test_system_prompt_has_discovery_fallback_instructions(self) -> None:
        result = load_system_prompt(None)
        assert "AGENTS.md" in result
        assert "CLAUDE.md" in result
        assert ".github/copilot-instructions.md" in result
        assert "pyproject.toml" in result
        assert "package.json" in result
        assert "go.mod" in result
        assert "Cargo.toml" in result

    def test_system_prompt_has_security_instructions(self) -> None:
        result = load_system_prompt(None)
        assert "untrusted data" in result or "untrusted" in result
        assert ".env" in result
        assert "~/.ssh" in result
        assert "KEY" in result
        assert "SECRET" in result
        assert "TOKEN" in result
        assert "PASSWORD" in result
        assert "curl" in result
        assert "wget" in result
        assert "printenv" in result

    def test_system_prompt_has_completion_contract(self) -> None:
        result = load_system_prompt(None)
        assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in result

    def test_system_prompt_forbids_the_agent_from_using_git(self) -> None:
        # The harness clones, branches, commits, pushes and opens the PR
        # (runner/executor.py). If the agent commits its own work, the
        # executor sees a clean tree and aborts as `no_changes`, discarding
        # it. The prompt must tell the agent to leave edits in the working
        # tree and never touch git.
        result = load_system_prompt(None)
        assert "git commit" in result
        assert "git push" in result
        assert "working tree" in result

    def test_system_prompt_allows_read_only_git_inspection(self) -> None:
        # The forbidden-git list above is a denylist of *mutating* verbs,
        # but read as a blanket ban it would deter the `git log -S` lookup
        # the "already fixed" case depends on. The allowance must be
        # explicit, and must not quietly re-permit a mutating command.
        result = load_system_prompt(None)
        assert "`git log`" in result
        assert "`git blame`" in result
        for forbidden in ("git commit", "git push", "git reset", "git checkout"):
            assert f"Reading history IS allowed and often useful: `{forbidden}`" not in result

    def test_system_prompt_requires_commit_provenance_when_already_fixed(self) -> None:
        # A run that ends in `no_changes` because the
        # work was already done used to describe only the current state of
        # the file, leaving a reviewer unable to tell "already fixed" from
        # "the model misread it" without redoing the archaeology by hand.
        result = load_system_prompt(None)
        assert "ALREADY present in the repository" in result
        # the recipe that actually finds the commit...
        assert "git log -S" in result
        # ...and the two things the report must carry.
        assert "SHA" in result
        assert "closed with no change" in result

    def test_already_fixed_case_forbids_inventing_a_sha(self) -> None:
        # Full history is the default, but `git.clone_depth` can bound it
        # (config.yaml `git:` block) and some text was simply never changed
        # as a unit. Either way the honest answer is "not found", never a
        # plausible-looking SHA.
        result = load_system_prompt(None)
        assert "Never invent or guess a SHA." in result

    def test_already_fixed_case_ends_via_the_completion_signal(self) -> None:
        # The agent has no `gh` and no token, so it cannot comment on the
        # issue itself. Only the submission path produces the text that
        # _build_abort_comment appends to the no_changes comment
        # (runner/executor.py), so this case must not say "comment and stop".
        result = load_system_prompt(None)
        already_fixed = result[result.index("ALREADY present in the repository") :]
        assert "completion signal" in already_fixed
        assert "working tree clean" in already_fixed

    # Regression: the agent adopted the issue's stated root cause
    # without testing it, then wrote a regression test asserting a value it
    # never measured. The tests below pin the instructions that prevent that.

    def test_bug_fix_treats_the_issues_stated_cause_as_a_hypothesis(self) -> None:
        text = _with_vocab(_read_prompt_file("bug_fix.md"))
        lowered = text.lower()
        assert "hypothesis" in lowered
        assert "falsify" in lowered
        # The issue's own claim is the thing under test, not an input.
        assert "cause the issue asserts" in lowered

    def test_bug_fix_prefers_the_tools_resolved_configuration(self) -> None:
        # The whole failure mode is a config file that says one thing and a
        # tool that resolves another, so reading the file is not evidence.
        text = _with_vocab(_read_prompt_file("bug_fix.md"))
        assert "resolved configuration" in text
        assert "--showConfig" in text

    def test_bug_fix_requires_reporting_when_the_cause_cannot_be_falsified(self) -> None:
        text = _with_vocab(_read_prompt_file("bug_fix.md"))
        assert "cannot falsify it cheaply" in text
        assert "final submission" in text

    def test_bug_fix_regression_test_must_not_assert_unmeasured_values(self) -> None:
        text = _with_vocab(_read_prompt_file("bug_fix.md"))
        regression = text[text.index("Add a regression test") : text.index("Run the test suite")]
        assert "reproduced" in regression
        assert "did not measure" in regression
        # A tuning value taken from the issue is reported, not enshrined.
        assert "unverified" in regression

    def test_bug_fix_keeps_its_original_steps(self) -> None:
        text = _with_vocab(_read_prompt_file("bug_fix.md"))
        for step in ("Reproduce the bug", "Apply the fix", "Run the test suite"):
            assert step in text

    def test_system_prompt_stops_on_an_unverifiable_fix(self) -> None:
        # The run in #92 reported "the full integration suite could not be
        # executed in this sandbox" and opened the PR anyway.
        result = load_system_prompt(None)
        start = result.index("CANNOT be verified in this sandbox")
        case = result[start : result.index("If you reach 80% of budget", start)]
        assert "premise" in case
        assert "do not edit anything" in case
        assert "proposed diff" in case
        assert "completion signal" in case
        assert "working tree clean" in case

    def test_unverifiable_fix_case_does_not_swallow_verifiable_fixes(self) -> None:
        # The stop applies only when BOTH the verification is impossible and
        # the premise is unverified; a fix the agent could confirm through
        # another route must still ship.
        result = load_system_prompt(None)
        start = result.index("CANNOT be verified in this sandbox")
        case = result[start : result.index("If you reach 80% of budget", start)]
        assert "AND" in case

    # ── Resource limits (the sandbox is a 1 GB, 1-shared-vCPU VM) ───────

    def _resource_limits(self) -> str:
        result = load_system_prompt(None)
        start = result.index("RESOURCE LIMITS")
        return result[start : result.index("SECURITY (fixed", start)]

    def test_system_prompt_warns_that_heavy_toolchains_are_slow_here(self) -> None:
        # The run on #247 launched tsc and jest side by side and sent the VM
        # into memory thrashing.
        section = self._resource_limits()
        assert "tsc" in section
        assert "jest" in section
        assert "1 GB" in section

    def test_system_prompt_runs_heavy_commands_one_at_a_time_in_the_foreground(self) -> None:
        section = self._resource_limits()
        assert "ONE heavy command at a time" in section
        assert "foreground" in section
        assert "in parallel" in section

    def test_system_prompt_forbids_detaching_processes(self) -> None:
        # The agent escaped the per-command kill with `setsid ... &`.
        section = self._resource_limits()
        for escape in ("&", "nohup", "setsid", "disown"):
            assert escape in section
        assert "killed" in section

    def test_system_prompt_tells_the_agent_its_node_options_are_ignored(self) -> None:
        section = self._resource_limits()
        assert "NODE_OPTIONS" in section
        assert "--max-old-space-size" in section
        assert "ignores" in section

    def test_system_prompt_says_to_narrow_a_command_that_timed_out(self) -> None:
        section = self._resource_limits()
        assert "do not relaunch" in section
        assert "--runTestsByPath" in section
        assert "tsc --noEmit -p" in section

    def test_system_prompt_reports_a_check_it_could_not_run_instead_of_retrying(self) -> None:
        section = self._resource_limits()
        assert "final summary" in section
        # Points at the existing stop rule rather than inventing a second one.
        assert "WHEN TO STOP" in section

    def test_resource_limits_sit_between_policy_and_security(self) -> None:
        result = load_system_prompt(None)
        assert (
            result.index("OPERATION POLICY")
            < result.index("RESOURCE LIMITS")
            < result.index("SECURITY (fixed")
        )

    def test_resource_limits_add_no_template_placeholder(self) -> None:
        # Every `{{ var }}` must exist at render time (StrictUndefined) — the
        # section states no numbers that would need one.
        assert "{{" not in self._resource_limits()

    def test_bug_fix_test_step_points_at_the_resource_limits(self) -> None:
        text = _with_vocab(_read_prompt_file("bug_fix.md"))
        start = text.index("**Run the test suite**")
        step = text[start : text.index("**Finish with the completion signal**", start)]
        assert "one at a time" in step
        assert "RESOURCE LIMITS" in step

    def test_diagnose_prompt_carries_the_same_limits(self) -> None:
        # A diagnosis may run the existing suite, in the very same sandbox.
        text = load_diagnose_system_prompt()
        assert "RESOURCE LIMITS" in text
        assert "ONE heavy command at a time" in text
        assert "setsid" in text

    def test_system_prompt_overrides_target_repo_git_instructions(self) -> None:
        # Target repos routinely ship CLAUDE.md/AGENTS.md that mandate
        # "create a branch, commit, run pre-commit". The prompt must say to
        # follow those for style/tests but ignore their git/PR steps.
        result = load_system_prompt(None)
        assert "CLAUDE.md" in result
        lowered = result.lower()
        assert "ignore" in lowered

    def test_system_prompt_does_not_tell_agent_to_open_a_pr_or_commit(self) -> None:
        result = load_system_prompt(None)
        assert "open a [DRAFT] PR" not in result
        assert "best partial commit" not in result
        assert "before\ncommitting" not in result and "before committing" not in result

    def test_bot_name_placeholder_replaces_hardcoded_name(self) -> None:
        result = load_system_prompt(None)
        assert "{{ bot_name }}" in result
        assert "Stateway Issue Agent" not in result

    def test_completion_contract_example_has_content_after_marker(self) -> None:
        # Regression: a bare `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
        # example produces an empty submission (the harness treats
        # everything after the marker's first line as the submission text),
        # silently breaking the "submission becomes the PR description"
        # feature. The example must show a marker line followed by real
        # content, inside a fenced example block, and the prompt must say
        # so explicitly.
        result = load_system_prompt(None)
        assert "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`" not in result, (
            "example must not be a bare marker with nothing after it"
        )

        fence_start = result.index("```")
        fence_end = result.index("```", fence_start + 3)
        example_block = result[fence_start:fence_end]
        assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in example_block

        block_lines = [line for line in example_block.splitlines() if line.strip()]
        # first content line holds the marker; there must be at least one
        # more non-blank line after it with actual summary text.
        assert len(block_lines) >= 3, (
            f"expected marker line plus summary content, got:\n{example_block}"
        )

        assert "first line becomes" in result

    def test_completion_contract_warns_against_echo_with_backticks(self) -> None:
        # Regression for a real production incident: the agent's summary
        # legitimately contained backtick-quoted code references, and since
        # the completion signal example used `echo "..."` with a
        # double-quoted string, the shell interpreted the backticks as
        # command substitution — corrupting the output ahead of the marker
        # line and silently breaking the completion signal. The example must
        # use a quoted heredoc (immune to backticks/`$`/escapes) and the
        # prompt must warn explicitly against the old `echo "..."` form.
        result = load_system_prompt(None)
        fence_start = result.index("```")
        fence_end = result.index("```", fence_start + 3)
        example_block = result[fence_start:fence_end]
        assert 'echo "' not in example_block, 'the example itself must not use echo "..."'
        assert "<<'SUBMIT_EOF'" in example_block
        assert "backtick" in result.lower()


class TestFullSuiteNote:
    def test_system_prompt_has_the_placeholder(self) -> None:
        assert "- Full test suite: {{ full_suite }}" in load_system_prompt(None)

    def test_monitored_leaves_the_full_suite_to_ci(self) -> None:
        note = full_suite_note(True, TERMS)
        assert f"runs in the {TERMS.ci} after your changes are pushed" in note
        assert "comes back to you with its log" in note
        assert "fast, targeted" in note
        assert f"not_run: <command> — left to the {TERMS.ci}" in note

    def test_unmonitored_keeps_verification_local(self) -> None:
        note = full_suite_note(False, TERMS)
        assert "nothing runs it after you" in note
        assert TERMS.ci not in note

    def test_uses_the_forges_own_name_for_ci(self) -> None:
        terms = FakeProvider.capabilities.terms.model_copy(update={"ci": "pipelines"})
        assert "runs in the pipelines" in full_suite_note(True, terms)


class TestLoadDiagnoseSystemPrompt:
    def test_has_content(self) -> None:
        result = load_diagnose_system_prompt()
        assert result.strip()

    def test_uses_bot_name_placeholder(self) -> None:
        result = load_diagnose_system_prompt()
        assert "{{ bot_name }}" in result

    def test_forbids_editing_files(self) -> None:
        result = load_diagnose_system_prompt()
        lowered = result.lower()
        assert "read-only" in lowered
        assert "do not edit" in lowered or "do not modify" in lowered

    def test_forbids_git_and_pr_actions(self) -> None:
        result = load_diagnose_system_prompt()
        assert "git commit" in result
        assert "git push" in result
        assert "Pull Request" in result

    def test_has_completion_contract(self) -> None:
        result = load_diagnose_system_prompt()
        assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in result

    def test_names_the_sentinel_file(self) -> None:
        result = load_diagnose_system_prompt()
        assert ".agent-diagnosis.json" in result

    def test_forbids_code_in_rationale(self) -> None:
        result = load_diagnose_system_prompt()
        lowered = result.lower()
        assert "never" in lowered
        assert "code" in lowered

    def test_names_all_categories(self) -> None:
        result = load_diagnose_system_prompt()
        for category in (
            "question",
            "feature_request",
            "high_severity_bug",
            "bug",
            "docs",
            "chore",
            "agent_fix_please",
        ):
            assert category in result

    def test_has_security_instructions(self) -> None:
        result = load_diagnose_system_prompt()
        assert "untrusted" in result.lower()
        assert ".env" in result

    def test_completion_contract_warns_against_echo_with_backticks(self) -> None:
        result = load_diagnose_system_prompt()
        # The marker is mentioned once in prose and once inside the fenced
        # example — the example is the LAST occurrence.
        marker_pos = result.rindex("COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")
        fence_start = result.rindex("```", 0, marker_pos)
        fence_end = result.index("```", marker_pos)
        example_block = result[fence_start:fence_end]
        assert 'echo "' not in example_block, 'the example itself must not use echo "..."'
        assert "<<'SUBMIT_EOF'" in example_block
        assert "backtick" in result.lower()


class TestInstanceTemplate:
    def test_has_only_value_placeholders(self) -> None:
        # {{ task }}, {{ comments }}, {{ resume_summary }} and
        # {{ branch_status }} are the ONLY Jinja expressions carrying data,
        # plus the plugin's static vocabulary, and there are no statement
        # tags — anything else would be a second injection surface.
        names = set(re.findall(r"\{\{ ([\w.]+) \}\}", INSTANCE_TEMPLATE))
        assert names == {
            "task",
            "comments",
            "resume_summary",
            "branch_status",
            "terms.issue",
            "terms.pr",
        }
        assert INSTANCE_TEMPLATE.count("{{") == len(
            re.findall(r"\{\{ [\w.]+ \}\}", INSTANCE_TEMPLATE)
        )
        assert "{{ task }}" in INSTANCE_TEMPLATE
        assert "{{ comments }}" in INSTANCE_TEMPLATE
        assert "{{ resume_summary }}" in INSTANCE_TEMPLATE
        assert "{{ branch_status }}" in INSTANCE_TEMPLATE
        assert "{%" not in INSTANCE_TEMPLATE

    def test_renders_malicious_branch_status_value_inert(self) -> None:
        # File paths in the branch status come from the target repo.
        status = build_branch_status(
            "conflicted", "dev", TERMS, [("{{ 7*7 }}.md", "both modified")]
        )
        rendered = Template(INSTANCE_TEMPLATE, undefined=StrictUndefined).render(
            **VOCAB, task="", comments="", resume_summary="", branch_status=status
        )
        assert "{{ 7*7 }}.md" in rendered
        assert "49" not in rendered

    def test_renders_malicious_resume_summary_value_inert(self) -> None:
        malicious = "{{ 7*7 }} {% for x in y %}{% endfor %} {{ LLM_API_KEY }}"
        rendered = Template(INSTANCE_TEMPLATE, undefined=StrictUndefined).render(
            **VOCAB, task="", comments="", resume_summary=malicious, branch_status=""
        )
        assert "{{ 7*7 }}" in rendered
        assert "49" not in rendered

    def test_renders_malicious_task_value_inert(self) -> None:
        malicious = "{{ 7*7 }} {% for x in y %}{% endfor %} {{ LLM_API_KEY }}"
        rendered = Template(INSTANCE_TEMPLATE, undefined=StrictUndefined).render(
            **VOCAB, task=malicious, comments="", resume_summary="", branch_status=""
        )

        assert "{{ 7*7 }}" in rendered
        assert "{% for x in y %}" in rendered
        assert "{{ LLM_API_KEY }}" in rendered
        assert "49" not in rendered

    def test_renders_malicious_comments_value_inert(self) -> None:
        malicious = "{{ 7*7 }} {% for x in y %}{% endfor %} {{ LLM_API_KEY }}"
        rendered = Template(INSTANCE_TEMPLATE, undefined=StrictUndefined).render(
            **VOCAB, task="", comments=malicious, resume_summary="", branch_status=""
        )

        assert "{{ 7*7 }}" in rendered
        assert "{% for x in y %}" in rendered
        assert "49" not in rendered

    def test_renders_without_raising_on_template_syntax_lookalikes(self) -> None:
        Template(INSTANCE_TEMPLATE, undefined=StrictUndefined).render(
            **VOCAB,
            task="{% this is not valid jinja",
            comments="{% also not }}",
            resume_summary="",
            branch_status="",
        )


class TestBuildBranchStatus:
    def test_fresh_branch_has_no_status(self) -> None:
        assert build_branch_status("", "dev", TERMS) == ""

    def test_conflict_lists_files_and_forbids_hand_porting(self) -> None:
        """Regression: told nothing, the agent copied the base's commits
        in by hand. The conflict, its files and the way out must be explicit."""
        status = build_branch_status(
            "conflicted",
            "dev",
            TERMS,
            [(".memory-bank/activeContext.md", "both modified"), ("src/a.ts", "deleted by us")],
            pr_number=288,
        )
        assert "Pull Request #288" in status
        assert "merged `origin/dev`" in status
        assert "  - .memory-bank/activeContext.md (both modified)" in status
        assert "  - src/a.ts (deleted by us)" in status
        assert "`git merge --abort`" in status
        assert "refuses to push if any marker is left" in status

    def test_clean_merge_says_nothing_is_left_to_bring_in(self) -> None:
        status = build_branch_status("merged", "main", TERMS)
        assert "merged `origin/main` into this branch cleanly" in status
        assert "do not copy the target branch's changes" in status
        assert "Pull Request" not in status

    def test_failed_update_is_reported(self) -> None:
        status = build_branch_status("failed", "dev", TERMS, pr_number=7)
        assert "could not merge `origin/dev`" in status
        assert "Pull Request #7" in status

    def test_a_published_branch_follow_up_names_no_pull_request_number(self) -> None:
        """A forge with no PR API (PRD §3.7): the branch itself is what was
        delivered, and there is no PR number to point at."""
        status = build_branch_status("merged", "main", TERMS, published_branch=True)
        assert "already published for review" in status
        assert "Pull Request #" not in status
        assert "merged `origin/main` into this branch cleanly" in status


class TestSystemPromptBranchUpdate:
    def test_points_at_branch_status_and_forbids_hand_porting(self) -> None:
        system = _with_vocab(_read_prompt_file("system.md"))
        assert "<branch_status>" in system
        assert "Never bring" in system
        assert "just make the requested code changes" not in system


class TestBuildUserMessage:
    def test_includes_issue_title_and_body(self) -> None:
        task = _make_task()
        msg = build_user_message(task, TERMS)
        assert "Fix typo in README" in msg
        assert "There is a typo on line 5" in msg

    def test_includes_issue_number(self) -> None:
        task = _make_task()
        msg = build_user_message(task, TERMS)
        assert "Issue #42" in msg

    def test_includes_author(self) -> None:
        task = _make_task()
        msg = build_user_message(task, TERMS)
        assert "@test-user" in msg

    def test_includes_labels(self) -> None:
        task = _make_task()
        msg = build_user_message(task, TERMS)
        assert "docs" in msg

    def test_includes_claude_md_reference(self) -> None:
        task = _make_task()
        msg = build_user_message(task, TERMS)
        assert "CLAUDE.md" in msg

    def test_handles_empty_title(self) -> None:
        task = _make_task(issue=make_issue(title=""))
        msg = build_user_message(task, TERMS)
        assert "(no title)" in msg

    @pytest.mark.parametrize("trigger_json", [None, "", "not valid json", "null"])
    def test_handles_missing_or_invalid_trigger(self, trigger_json) -> None:
        task = make_task(trigger_json=trigger_json)
        msg = build_user_message(task, TERMS)
        assert "(no title)" in msg
        assert "@unknown" in msg

    def test_handles_no_body(self) -> None:
        task = _make_task(issue=make_issue(body=""))
        msg = build_user_message(task, TERMS)
        assert "(no body)" in msg

    def test_handles_empty_labels_list(self) -> None:
        task = _make_task(issue=make_issue(labels=[]))
        msg = build_user_message(task, TERMS)
        assert "Labels" not in msg

    def test_handles_no_actor(self) -> None:
        task = _make_task(actor="")
        msg = build_user_message(task, TERMS)
        assert "@unknown" in msg

    def test_author_is_the_actor(self) -> None:
        task = _make_task(actor="labeler", author=make_author(login="reporter"))
        msg = build_user_message(task, TERMS)
        assert "@labeler" in msg

    def test_multiline_body_is_preserved(self) -> None:
        task = _make_task(issue=make_issue(number=1, title="Test", body="Line 1\nLine 2\nLine 3"))
        msg = build_user_message(task, TERMS)
        assert "Line 1" in msg
        assert "Line 2" in msg
        assert "Line 3" in msg


class TestBuildCommentsBlock:
    def test_empty_when_no_trigger_and_no_history(self) -> None:
        task = _comment_task(trigger_comment=None)
        assert build_comments_block(task, [], _settings()) == ""

    def test_trigger_comment_from_issue_author_is_included(self) -> None:
        task = _comment_task(
            trigger_comment=_trigger_comment("9", "actually the bug is in the parser"),
            issue_author="reporter",
        )
        block = build_comments_block(task, [], _settings())
        assert "actually the bug is in the parser" in block
        assert "@reporter" in block
        assert "triggered this run" in block

    @pytest.mark.parametrize(
        "association,label", [("member", "MEMBER"), ("none", "NONE"), ("unknown", "NONE")]
    )
    def test_trigger_header_renders_association_as_before(self, association, label) -> None:
        comment = make_comment(
            id="9",
            author=make_author(login="maintainer", association=association),
            body="go",
            created_at="2026-09-08T10:00:00Z",
        )
        task = _comment_task(trigger_comment=comment)
        block = build_comments_block(task, [], _settings())
        assert block.startswith(
            f"[@maintainer · {label} · 2026-09-08T10:00:00+00:00 · triggered this run]"
        )

    def test_history_keeps_members_drops_third_parties(self) -> None:
        task = _comment_task(trigger_comment=None, issue_author="reporter")
        history = [
            _history_comment(1, "maintainer", "member", "looking into it"),
            _history_comment(2, "drive-by", "none", "me too!!!"),
        ]
        block = build_comments_block(task, history, _settings())
        assert "looking into it" in block
        assert "me too!!!" not in block

    def test_history_header_renders_canonical_association(self) -> None:
        task = _comment_task(trigger_comment=None, issue_author="reporter")
        history = [_history_comment(1, "maintainer", "member", "looking into it")]
        block = build_comments_block(task, history, _settings())
        assert block.startswith("[@maintainer · MEMBER · 2026-09-08T09:01:00+00:00]")

    def test_history_with_unknown_association_needs_another_pass(self) -> None:
        # A forge that doesn't report associations: only the issue author
        # and the allowlist can clear a history comment (PRD §3.1).
        task = _comment_task(trigger_comment=None, issue_author="reporter")
        history = [
            _history_comment(1, "maintainer", "unknown", "from a maintainer"),
            _history_comment(2, "reporter", "unknown", "from the reporter"),
        ]
        block = build_comments_block(task, history, _settings())
        assert "from a maintainer" not in block
        assert "[@reporter · NONE ·" in block

    def test_agents_own_past_comment_is_included_and_marked(self) -> None:
        task = _comment_task(trigger_comment=None, issue_author="reporter")
        history = [
            _history_comment(
                5, "issue-agent[bot]", "none", "I opened PR #7 with a fix.", is_self=True
            )
        ]
        block = build_comments_block(task, history, _settings())
        assert "I opened PR #7 with a fix." in block
        assert "you" in block.lower()

    def test_own_comment_is_kept_even_with_filter_rejecting_its_author(self) -> None:
        task = _comment_task(trigger_comment=None, issue_author="reporter")
        history = [_history_comment(6, "", "unknown", "Attempt 1 aborted.", is_self=True)]
        block = build_comments_block(task, history, _settings())
        assert "Attempt 1 aborted." in block
        # A missing login falls back to the configured app name.
        assert f"@{_settings().agent_name} · you wrote this earlier" in block

    def test_body_is_truncated_with_marker(self) -> None:
        task = _comment_task(trigger_comment=None, issue_author="reporter")
        history = [_history_comment(1, "maintainer", "member", "x" * 5000)]
        block = build_comments_block(
            task, history, _settings(COMMENTS_CONTEXT_MAX_CHARS_PER_COMMENT=100)
        )
        assert "x" * 100 in block
        assert "x" * 200 not in block
        assert "truncated" in block.lower()

    def test_trigger_comment_deduped_from_history_by_id(self) -> None:
        trigger = _trigger_comment("42", "one and only")
        task = _comment_task(trigger_comment=trigger, issue_author="reporter")
        history = [_history_comment(42, "reporter", "none", "one and only")]
        block = build_comments_block(task, history, _settings())
        assert block.count("one and only") == 1

    def test_history_capped_at_max_comments(self) -> None:
        task = _comment_task(trigger_comment=None, issue_author="reporter")
        history = [
            _history_comment(i, "maintainer", "member", f"comment number {i}") for i in range(1, 11)
        ]
        block = build_comments_block(task, history, _settings(COMMENTS_CONTEXT_MAX_COMMENTS=3))
        assert "comment number 10" in block
        assert "comment number 8" in block
        assert "comment number 7" not in block

    def test_malicious_comment_body_is_carried_verbatim_as_value(self) -> None:
        task = _comment_task(
            trigger_comment=_trigger_comment("1", "{{ 7*7 }} {% for x in y %}{% endfor %}"),
            issue_author="reporter",
        )
        block = build_comments_block(task, [], _settings())
        rendered = Template(INSTANCE_TEMPLATE, undefined=StrictUndefined).render(
            **VOCAB, task="", comments=block, resume_summary="", branch_status=""
        )
        assert "{{ 7*7 }}" in rendered
        assert "49" not in rendered


class TestProviderVocabulary:
    """No prompt names a forge: the vocabulary comes from the plugin's
    ``capabilities.terms`` (PRD §3.2, Phase 4). The FakeProvider's terms
    ("ticket", "Merge Request", "!") make a leftover GitHub word visible."""

    FAKE_TERMS = FakeProvider.capabilities.terms
    _FORGE_WORDS = re.compile(r"GitHub|Pull Request|copilot|\bPRs?\b|\bissues?\b")

    @staticmethod
    def _render(template: str, terms) -> str:
        return Template(template, undefined=StrictUndefined).render(
            bot_name="Issue Agent",
            test_command="",
            lint_command="",
            protected_paths=[],
            relevant_extensions=[],
            # The monitored note: the longer one, checked for forge words too.
            full_suite=full_suite_note(True, terms),
            task="",
            comments="",
            resume_summary="",
            branch_status="",
            **prompt_vars(terms),
        )

    def _all_prompts(self) -> list[str]:
        return [
            *(
                _load_system_prompt(c, DEFAULT_CATEGORY_PROMPT_MAP)
                for c in [None, *DEFAULT_CATEGORY_PROMPT_MAP]
            ),
            _load_diagnose_system_prompt(),
            INSTANCE_TEMPLATE,
        ]

    @pytest.mark.parametrize(
        "name", ["system.md", "system_diagnose.md", "bug_fix.md", "doc_fix.md"]
    )
    def test_prompt_files_name_no_forge(self, name: str) -> None:
        text = re.sub(r"\{\{ [\w.]+ \}\}", "", _read_prompt_file(name))
        assert not self._FORGE_WORDS.findall(text)

    def test_every_prompt_renders_with_another_forges_vocabulary(self) -> None:
        for template in self._all_prompts():
            rendered = self._render(template, self.FAKE_TERMS)
            # <issue> is the data delimiter, not vocabulary.
            assert not self._FORGE_WORDS.findall(re.sub(r"</?issue>", "", rendered))
            assert "ticket" in rendered

    def test_fix_prompt_speaks_the_forges_vocabulary(self) -> None:
        rendered = self._render(
            _load_system_prompt("bug", DEFAULT_CATEGORY_PROMPT_MAP), self.FAKE_TERMS
        )
        assert "Merge Request description" in rendered
        assert "`(!123)`" in rendered
        assert "`AGENTS.md` or `CLAUDE.md`" in rendered

    def test_github_rendering_keeps_its_vocabulary(self) -> None:
        rendered = self._render(_load_system_prompt(None, DEFAULT_CATEGORY_PROMPT_MAP), TERMS)
        assert "resolve issues" in rendered
        assert "Pull Request description" in rendered
        assert "`(#123)`" in rendered
        assert "`.github/copilot-instructions.md`" in rendered

    def test_instructions_files_lists_the_plugins_files_after_the_core_ones(self) -> None:
        assert format_instructions_files(TERMS) == (
            "`AGENTS.md`, `CLAUDE.md`, or `.github/copilot-instructions.md`"
        )
        assert format_instructions_files(self.FAKE_TERMS) == "`AGENTS.md` or `CLAUDE.md`"

    def test_user_message_uses_the_forges_reference_syntax(self) -> None:
        msg = build_user_message(_make_task(), self.FAKE_TERMS)
        assert "Ticket #42: Fix typo in README" in msg
        assert "--- Ticket Body ---" in msg
        assert "Issue" not in msg

    def test_branch_status_names_the_forges_pr(self) -> None:
        status = build_branch_status("merged", "main", self.FAKE_TERMS, pr_number=288)
        assert "open Merge Request !288" in status
        assert "Pull Request" not in status
        published = build_branch_status("merged", "main", self.FAKE_TERMS, published_branch=True)
        assert "with no Merge Request on it" in published
