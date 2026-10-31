# Label Schema

**Status:** normative — reflects `policy/label_router.py`, `config.yaml`'s
`policy:` block, and `config.yaml`'s `prompts.category_map` block.
**Source of truth:** `src/issue_agent/policy/label_router.py`
(`get_label_category`, `_classify_labels`) resolves *categories*; which
category routes to which action is configured by `Settings.policy_triage_only_categories`
/ `Settings.policy_diagnose_categories` (`config.py`), not fixed in code.

This is the contract an issue repository opts into. Any repo that wants the
agent to act on its issues should create these labels and wire its issue
templates to apply them automatically. The label names are fixed in code;
the colours and descriptions below are a suggestion, and which action each
category triggers is configurable (see §1.1).

## 1. What the agent does per label

`_classify_labels` resolves each issue to one of four actions:

| Action | Meaning |
|---|---|
| `FIX` | The agent clones the repo, attempts a fix, and opens a Pull Request. It never merges. |
| `DIAGNOSE` | The agent clones the repo and inspects it **read-only**, then posts a verdict on whether the issue's label matches reality. It never edits files, commits, or opens a PR. |
| `TRIAGE_ONLY` | The agent posts a templated acknowledgement comment and stops — no LLM call at all. A human decides. No branch, no PR. |
| `SKIP` | The agent ignores the issue entirely. |

| Label | Suggested colour | Default action | Notes |
|---|---|---|---|
| `bug` | `#D73A4A` | `FIX` | Unless promoted to `TRIAGE_ONLY` by a severity label — see §3. |
| `docs` | `#008672` | `FIX` | Documentation / README / API reference edits. |
| `chore` | `#FBBD2F` | `FIX` | Light refactor, lint, dependency bump. |
| `question` | `#D876E3` | `TRIAGE_ONLY` | The agent acknowledges and defers to the team. |
| `feature-request` | `#0E8A16` | `TRIAGE_ONLY` | The agent records it; humans decide whether to build it. |
| `agent-fix-please` | `#FFFFFF` | `FIX` (override) | Forces `FIX` on an issue that carries no other actionable label. |
| `agent-skip` | `#000000` | `SKIP` (override) | Forces `SKIP` even when actionable labels are present. Wins over everything. |

An issue with **no** label in this table resolves to `SKIP`.

### 1.1 Which categories route to which action is configurable

`Settings.policy_triage_only_categories` and `Settings.policy_diagnose_categories`
(`config.yaml`'s `policy:` block) list which of the seven categories below
route to `TRIAGE_ONLY` / `DIAGNOSE`. The defaults reproduce the table
above exactly:

```yaml
policy:
  triage_only_categories: [question, feature_request, high_severity_bug] # default
  diagnose_categories: [] # default — DIAGNOSE is opt-in
```

A category not listed in either key keeps its built-in `FIX`/`SKIP`
behaviour (`bug`, `docs`, `chore`, `agent_fix_please` → `FIX`; anything
unrecognized, or `agent-skip` → `SKIP`). Moving a category into
`diagnose_categories` overrides its default — e.g. setting
`diagnose_categories: [bug]` routes every plain `bug` issue to `DIAGNOSE`
instead of `FIX`. The two lists must not overlap, and every entry must be
one of the seven category names (§2); the process refuses to start
otherwise. See `config.yaml.example` for the full documented block.

### Precedence

`agent-skip` is checked first and always wins. Otherwise the category is
resolved by first match in this order:

```
question > feature-request > bug (severity-qualified) > docs > chore > agent-fix-please
```

Once a category is resolved, the action lookup checks `diagnose_categories`
before `triage_only_categories` before the built-in `FIX` set — so a
category placed in `diagnose_categories` wins even if it is also (by
misconfiguration) still listed as triage-only.

### When a label change becomes a task

Adding a label (`issues.labeled`) is judged on the issue's **full current
label set**, not on the label just added. It enqueues a task only when that
set carries a category label (any of the seven, §2) or `agent-skip`
(`label_router.py::labels_can_trigger`). Otherwise the webhook drops it
with `reason: no_trigger_label`: adding `wontfix` or `priority:low` to an
otherwise unlabeled issue posts nothing. Adding an unrelated label to an
issue that already carries `bug` still enqueues — the set has a trigger
label. `issues.opened` is not filtered this way: an issue opened with no
recognized label still gets the `SKIP` comment.

## 2. Label → prompt category

For `FIX` issues the resolved category selects the system-prompt template
(`config.yaml` `prompts.category_map` → `prompts/<file>`):

| Category | Default action | Prompt template |
|---|---|---|
| `bug` | `FIX` | `bug_fix.md` |
| `docs` | `FIX` | `doc_fix.md` |
| `chore` | `FIX` | `bug_fix.md` |
| `agent_fix_please` | `FIX` | `bug_fix.md` |
| `question` | `TRIAGE_ONLY` | — (uses `triage/builder.py`, no LLM) |
| `feature_request` | `TRIAGE_ONLY` | — (uses `triage/builder.py`, no LLM) |
| `high_severity_bug` | `TRIAGE_ONLY` | — (uses `triage/builder.py`, no LLM) |

The three categories with no `category_map` entry route to `TRIAGE_ONLY` by
default and never instantiate the agent — that path builds a Python-templated
comment via `triage/builder.py` instead. This is independent of `prompts.category_map`:
whichever categories end up in `policy.diagnose_categories` run the agent under
a dedicated read-only prompt (`prompts/system_diagnose.md`, loaded via
`load_diagnose_system_prompt()`), never a `category_map` entry — a `DIAGNOSE`
run never edits files, so there is nothing for `bug_fix.md`/`doc_fix.md` to say.

## 3. Severity — via labels, not the issue body

Severity is expressed as **GitHub labels**, not a field parsed out of the
issue body. Two labels matter:

| Label | Suggested colour | Effect on a `bug` issue |
|---|---|---|
| `severity:high` | `#B60205` | Promotes the issue to `high_severity_bug` → `TRIAGE_ONLY` by default |
| `severity:critical` | `#B60205` | Same as `severity:high` |

A `bug` issue with **neither** label (or with a `severity:low` label, which
the router simply ignores) stays in category `bug` → `FIX` by default.
Severity labels have no effect on non-`bug` issues. Whether `high_severity_bug`
actually lands on `TRIAGE_ONLY` (rather than `DIAGNOSE`, or `FIX` if removed
from both lists) is governed by §1.1's config, same as any other category.

> **History:** an earlier draft of this document proposed reading a
> `Severity:` field from the issue body and creating no `severity:*` labels.
> That was never implemented; the code has always keyed off labels. The
> label-based behaviour above is the resolved contract.

## 4. DIAGNOSE — checking a classification without fixing anything

`DIAGNOSE` runs the real coding agent against a read-only clone of the
repo — it may read files, run the test suite, reproduce behaviour — but a
dedicated system prompt (`prompts/system_diagnose.md`) forbids editing
files, git operations, and opening a PR, and the harness never gives it a
credentialed remote or calls anything from the PR-creation code path
(`runner/executor.py`'s `run_task`, not `run_diagnosis`, owns that).

The agent reports its verdict by writing a single JSON file,
`.agent-diagnosis.json`, to the workspace root:

```json
{"actual_category": "bug", "confidence": "high", "rationale": "..."}
```

The harness (`runner/diagnose.py`) reads and deletes this file, then builds
the issue comment **entirely from the two enum fields** — `actual_category`
and `confidence` — never from the agent's free-form final output. The one
piece of prose, `rationale`, is sanitized (`diagnose/verdict.sanitize_rationale`)
to strip anything that looks like a code block or a long inline code span
before it can appear in the public comment. If the sentinel file is missing
or fails validation, the comment reports an inconclusive diagnosis instead
of guessing. No label is ever added or removed automatically — a human
relabels the issue if they agree with the verdict.

`DIAGNOSE` is opt-in (`policy.diagnose_categories` defaults to `[]`) and
reuses the same global cost/iteration/timeout budgets as `FIX`
(`budgets:` in `config.yaml`) — there is no separate `DIAGNOSE` budget.

## 5. Wiring an issue repository (example)

A consumer repo typically ships GitHub issue-form templates
(`.github/ISSUE_TEMPLATE/*.yml`) that apply the right label automatically:

| Template | `labels:` | Agent behaviour (default config) |
|---|---|---|
| `bug_report.yml` | `bug` | `FIX` — or `TRIAGE_ONLY` if a maintainer adds `severity:high`/`severity:critical` |
| `feature_request.yml` | `feature-request` | `TRIAGE_ONLY` |
| `docs_change.yml` | `docs` | `FIX` |
| `question.yml` | `question` | `TRIAGE_ONLY` |
| `chore.yml` | `chore` | `FIX` |

Notes for the template author:

- Issue forms cannot apply a label conditionally, so severity is **not** a
  template dropdown that maps to a label. Keep severity out of the form, or
  make it a plain informational dropdown and let a maintainer apply
  `severity:high` / `severity:critical` when triaging.
- `agent-fix-please` and `agent-skip` are maintainer tools, not template
  labels.
- Keep `blank_issues_enabled: false` so every issue arrives with a label.
- The label a template applies must match the category name in
  `label_router.py` exactly (`feature-request`, not `enhancement` — GitHub's
  own default label is a different string and does not route anywhere).

## 6. Checklist for onboarding a new issue repo

- [ ] Create the labels in §1 and §3 (`bug`, `docs`, `chore`, `question`,
      `feature-request`, `agent-fix-please`, `agent-skip`, `severity:high`,
      `severity:critical`).
- [ ] Add issue-form templates that apply the label automatically (§5).
- [ ] Install the GitHub App on the repo (and on the target/code repo if
      different).
- [ ] Add the pair to `config.yaml` under `repos:`.
- [ ] Decide whether any category should route to `DIAGNOSE` instead of its
      default (§1.1, §4) — most repos can leave `policy:` unset.
- [ ] Open one issue per template and confirm the agent classifies it as
      expected (watch the worker logs / `/metrics`).
