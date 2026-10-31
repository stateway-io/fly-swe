This {{ terms.issue }} has been classified as **documentation**. Follow the flow below:

1. **Identify the relevant documentation file** — README, docs/, API specs
   (.yaml/.json), inline code comments/docstrings, or error messages in
   code.
2. **Apply the fix** — smallest change possible.
3. **Validate** — confirm that no source code files were modified (relevant
   extensions for this project: {{ relevant_extensions }}; if that's empty,
   use whatever source extensions you identified while exploring the repo).
   If the {{ terms.issue }} also requires a code change, comment on the {{ terms.issue }}
   requesting separation between the doc fix and the code fix.
4. **Run lint** — `{{ lint_command }}` (documentation may trigger
   markdownlint or similar rules). If empty, discover it: check
   {{ instructions_files }} at the
   repo root first; if none exist, infer from ecosystem markers
   (`pyproject.toml`/`setup.py`, `package.json`, `go.mod`, `Cargo.toml`,
   etc.).
5. **Finish with the completion signal** described in the system prompt;
   the system builds the {{ terms.pr }} from your final submission.

RESTRICTIONS:
- Modify ONLY documentation files.
- Do not change tests, source code, configurations, or pipelines.
- If the documentation describes behavior that no longer matches the code,
  open a separate {{ terms.issue }} for the code fix and resolve only the documentation
  here.
