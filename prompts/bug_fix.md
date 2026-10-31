This {{ terms.issue }} has been classified as a **bug** with low severity. Follow the flow below:

1. **Reproduce the bug** — identify the minimum conditions to reproduce.
2. **Identify the root cause** — understand why the expected behavior does not occur.
3. **Falsify the cause the {{ terms.issue }} asserts** — a reproduced symptom is usually
   real even when the cause the reporter gives for it is wrong, so treat that
   cause as a hypothesis, not as input:
   - State, in one sentence, the cause the {{ terms.issue }} asserts (and any cause it
     explicitly rules out).
   - Find the cheapest command that would falsify it, and run it before you
     touch any file. Prefer a read-only command; it is almost always cheaper
     than anything else you will run.
   - For configuration-driven defects, dump the tool's
     **resolved configuration** instead of reading the config file — the
     failure mode is a file that says one thing while the tool resolves
     another (for example `jest --showConfig`, `tsc --showConfig`,
     `eslint --print-config`, `git config --list`).
   - If the hypothesis is falsified, fix the real cause and say so in your
     final submission. If you cannot falsify it cheaply, say that in your
     final submission too — never leave it implicit.
4. **Apply the fix** — smallest change that resolves the root cause.
5. **Add a regression test** — the test must capture the bug scenario
   to prevent it from returning in the future. It may assert the behavior you
   reproduced; it must NOT assert a threshold, constant, or configuration
   value that you did not measure yourself. If the fix is a tuning value
   taken from the {{ terms.issue }}, report in your final submission that the value is
   unverified rather than encoding it in a test as an invariant: a test built
   on an unverified premise turns a guess into a rule that CI enforces and
   the next reader takes for a deliberate decision.
6. **Run the test suite** — `{{ test_command }}` and `{{ lint_command }}`
   must pass. If either is empty, discover it: check
   {{ instructions_files }} at the repo root
   first; if none exist, infer from ecosystem markers
   (`pyproject.toml`/`setup.py` → pytest, `package.json` → the scripts
   already defined in it, `go.mod` → `go test`, `Cargo.toml` →
   `cargo test`, etc.). Run heavy checks one at a time, in the foreground,
   starting with the narrowest one that covers your change (see the
   RESOURCE LIMITS section of the system prompt).
7. **Finish with the completion signal** described in the system prompt;
   the system builds the {{ terms.pr }} from your final submission.

RESTRICTIONS:
- Do not modify files under the protected paths: {{ protected_paths }}.
  If that list is empty, rely on what you discovered via the project's own
  instructions ({{ instructions_files }}) — do not invent
  restrictions that aren't documented anywhere.
- If the fix requires a change in a protected path, comment on the {{ terms.issue }}
  with a proposal and flag for human review — do not try to work around it.
- Prefer localized and conservative changes. If the scope grows,
  stop and propose an alternative approach.
