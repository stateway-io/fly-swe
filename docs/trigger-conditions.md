# Agent trigger conditions

This document explains, precisely, when the fly-swe agent actually runs. It is
not a single config gate: `config.yaml` controls *which repo* is watched and
*how* the agent behaves once it runs, but whether a given GitHub event turns
into an agent invocation is decided by a four-stage pipeline spread across the
webhook handler, an ingestion-time comment filter, the SQLite task queue, and
worker-time guards plus label classification. Several of these stages have
defaults that are **not visible in the live `config.yaml`** because the file
omits the sections that would override them.

All line references are current as of this writing; re-check them if the
referenced files have since changed.

## Pipeline overview

```
GitHub webhook → POST /webhooks/{instance_id} (the `github` instance)
  → provider lookup by path, delivery key, signature, dedup          [routes.py, providers/registry.py]
  → normalization into a TriggerEvent: event allowlist, trigger-kind
    inference (issues.opened/labeled, issue_comment.created,
    issues.closed/reopened)                                          [providers/github/webhook.py]
  → repo allowlist                                                   [routes.py]
  → core ingestion filters (PR comment, own-bot, closed issue,
    comment author, label set without a trigger label)               [policy/ingestion.py, comment_filter.py]
  → issue lifecycle short-circuit (closed/reopened: no task enqueued) [routes.py]
  → Task row inserted into SQLite, status=PENDING                    [routes.py]
  → worker dequeues (round-robin across repos)                       [worker.py, store.py]
  → worker-time guards: payload, repo, author, daily budget          [guards.py]
  → label classification: SKIP / TRIAGE_ONLY / DIAGNOSE / FIX         [label_router.py, config.py's policy: block]
  → FIX path: R7.6 stale-closed re-check, then clone + LLM loop       [executor.py]
  → DIAGNOSE path: clone + read-only LLM loop, verdict comment only   [runner/diagnose.py]
```

### Stage-by-stage detail

1. **HTTP entrypoint** — `POST /webhooks/{instance_id}`
   (`webhook/routes.py::handle_webhook`). The provider instance comes from
   the path, never from headers (Gitea/Forgejo also send `X-GitHub-*`); the
   GitHub App's URL `/webhooks/github` is the `github` instance
   (`providers/registry.py`). An unknown instance → HTTP 404
   `{"error":"unknown provider instance"}`, with nothing recorded. The
   handler then asks the instance's `WebhookAdapter`
   (`providers/github/webhook.py::GitHubWebhookAdapter`), in order:
   - `delivery_key`: requires the `X-GitHub-Delivery` header, else HTTP 400.
   - `verify_signature`: HMAC-SHA256 against the instance's
     `PROVIDERS__<ID>__WEBHOOK_SECRET`, else
     HTTP 400 — and the delivery is **not** recorded, so a forged request
     can't burn a real delivery id. An empty secret rejects every request
     (fail-closed).
   - Duplicate delivery key short-circuits to `200 {"status":"duplicate"}`
     (`queue/store.py::is_delivery_processed`). This runs before the event
     is parsed, so a redelivered unsupported event also answers
     `duplicate`.
   - `parse_event`: only `issues` and `issue_comment` are accepted; any
     other signed event (`pull_request`, `push`, `check_suite`, `ping`, …) →
     `200 {"status":"ignored","reason":"unsupported_event"}`. A body that
     isn't JSON → 400 `invalid JSON payload`; JSON that can't be normalized
     → 400 `invalid payload` (both recorded as processed).
   - Repo must be in `config.yaml`'s `repos[].issues` list, or the event is
     dropped with `reason: repo_not_allowed` (`Settings.route_for`).
   - `issues.closed` / `issues.reopened` are handled inline — **no Task is
     ever enqueued** for these (`routes.py::_handle_issue_lifecycle`).
   - Any other action on an allowed event that doesn't map to a trigger kind
     (e.g. `issues.assigned`, `issues.edited`, `issues.deleted`) is dropped
     with `reason: unhandled_event_action`.

2. **Trigger-kind mapping** — `_infer_trigger_kind()`
   (`providers/github/webhook.py::_infer_trigger_kind`):

   | event | action | `TriggerKind` |
   |---|---|---|
   | `issues` | `opened` | `ISSUE_OPENED` |
   | `issues` | `labeled` | `LABEL_ADDED` |
   | `issue_comment` | `created` | `COMMENT_COMMAND` |
   | `issues` | `closed` | `ISSUE_CLOSED` (no task) |
   | `issues` | `reopened` | `ISSUE_REOPENED` (no task) |
   | anything else | any | `Ignored("unhandled_event_action")` |

3. **Comment-specific ingestion filters** (`policy/ingestion.py::ingestion_filter`,
   a core module run by the handler on the canonical `TriggerEvent` the
   adapter produced — the adapter itself only normalizes), evaluated in order,
   each a silent drop with no task and no cost:
   - **PR-comment filter**: the adapter sets `TriggerEvent.target_kind="pr"`
     when the payload has an `issue.pull_request` key —
     present only when GitHub's `issue_comment` event fires for a comment on
     a PR's "Conversation" tab rather than a genuine issue (GitHub treats
     PRs as a superset of issues, so the same event type covers both). A
     match is dropped with `reason: comment_on_pull_request`. This check is unconditional — it does not
     depend on the issues/target repo split, so it still holds even when an
     operator points both at the same repo. Reviews on the agent's own PRs
     are a distinct, not-yet-built feature (see the code comment at the
     check site for the intended future direction).
   - **Own-bot filter**: a comment authored by fly-swe's own GitHub App
     (matched by `performed_via_github_app.id` or `type=="Bot"` +
     slugified `{agent.name}[bot]` login, or `providers.<id>.bot_login`) is
     dropped. `providers/github/normalize.py` records this as `Author.is_self`.
   - **Closed-issue filter**: a comment on an issue with `state=="closed"`
     is dropped.
   - **Comment-author allowlist**: `comment_author_allowed()`
     (`policy/comment_filter.py:22-61`), controlled by
     `comments.author_filter.*` in `config.yaml`. Passes if *any* of:
     filter disabled; commenter is the issue's original author (even if
     that author is itself a bot); the commenter's canonical
     `Author.association` is in `allow_associations` (default
     `OWNER`/`MEMBER`/`COLLABORATOR`, compared case-insensitively); or
     login is on the explicit `allowlist`. The agent's own comments never
     pass. A rejected comment is dropped with
     `reason: comment_author_not_allowed` — no task, no reply comment, no
     LLM cost. GitHub's `author_association` is mapped to the canonical
     set by `providers/github/normalize.py::map_association`; note that
     `FIRST_TIME_CONTRIBUTOR` maps to `contributor`, and
     `FIRST_TIMER`/`MANNEQUIN` to `none`. When the payload carries **no**
     association (canonical `unknown`), the webhook can't judge it without
     a network call: the event is enqueued and the worker's
     `check_comment_author_allowed()` guard decides (step 5).

   Note: despite the name `COMMENT_COMMAND`, there is **no parsing of the
   comment body** anywhere in the codebase — any text from an allowed
   author on a genuine (non-PR) issue triggers a run.

4. **Task enqueue** (`routes.py`): a `LABEL_ADDED` event whose current
   label set has no trigger label — no category label and no
   `agent-skip` (`label_router.py::labels_can_trigger`) — is dropped with
   `reason: no_trigger_label`, no task. Otherwise the trigger's labels are
   mapped to a `label_category`, the event's credentials hint is stored as
   `credentials_ref` (GitHub: the payload's `installation.id`; nothing reads
   it — the plugin resolves each repo's credentials in the worker, and the
   handler makes no network call),
   and a `Task(status=PENDING)` row is written to SQLite. The delivery is
   marked processed only *after* the insert succeeds, so a mid-flight
   failure leaves the delivery eligible for GitHub's automatic retry.

5. **Worker dequeue and guards** — `worker.py` polls every
   `worker.poll_interval_seconds` (2.0s in `config.yaml`) and processes one
   task at a time, round-robining across repos
   (`queue/store.py:330-338`). `run_task()` (`worker.py:396-641`) runs, in
   order, aborting on the first failure:
   1. `validate_task_payload()` — the task's canonical trigger
      (`Task.trigger`, deserialized from `tasks.trigger_json`) must exist
      and name an issue number and repo, else `FAILED_TERMINAL`
      (`policy/guards.py`).
   2. `check_repo_allowed()` — re-checks `task.repo` against
      `settings.allowed_repos` (derived from `config.yaml`'s `repos:`
      list) → `ABORTED_POLICY` if not (`guards.py:31-34`).
   3. `check_author_allowed()` — only enforced if `settings.allowed_authors`
      is non-empty (env var `ALLOWED_AUTHORS`; **not set** in this
      deployment) — otherwise a no-op. It checks `TriggerEvent.actor`,
      whoever caused the event (GitHub's `sender`: on `labeled`, the
      labeler, not the issue author) (`guards.py`).
   4. `check_comment_author_allowed()` — the half of the comment-author
      filter the webhook deferred: a `COMMENT_COMMAND` whose author
      association is `unknown` passes only as the issue author or an
      allowlisted login, else `ABORTED_POLICY` with
      `error: comment_author_not_allowed` and **no** comment, as silent as
      the ingestion drop it stands in for (`guards.py`). A known
      association was already judged at ingestion and passes.
   5. `check_daily_budget()` — if the target repo's spend today is at or
      above `budgets.max_daily_cost_usd` (5.00 in `config.yaml`), the task
      is **requeued** (not aborted) and retried automatically once budget
      allows (`guards.py:56-66`; `worker.py:459-507`).

   Every guard failure except the comment-author abort and the
   budget-requeue posts a
   `🤖 … task not processed` comment on the issue
   (`worker.py:423,439,455`).

6. **Label classification** — `classify_labels()` (`policy/label_router.py`)
   over the trigger's current label set (`TriggerEvent.issue.labels`), run
   only after guards pass, fed `settings.policy_triage_only_categories` /
   `settings.policy_diagnose_categories` from `worker.py`:
   - `agent-skip` label present → `SKIP`, regardless of any other label.
   - No recognized label at all → `SKIP`.
   - Category resolves and is listed in `policy.diagnose_categories`
     (empty by default) → `DIAGNOSE` — the agent inspects the repo
     read-only and posts a verdict comment; no code changes, no PR.
   - Category resolves and is listed in `policy.triage_only_categories`
     (default: `question`, `feature-request`, `high_severity_bug`) →
     `TRIAGE_ONLY` (templated comment posted, zero LLM cost).
   - `bug` (no high/critical severity, unless moved into one of the lists
     above), `docs`, `chore`, or `agent-fix-please` → `FIX` (the actual
     coding-agent run that can open a PR).
   - Precedence when multiple labels are present (first match wins):
     `question` > `feature-request` > `bug` (severity-qualified) > `docs`
     > `chore` > `agent-fix-please` > none. Once a category is resolved,
     `diagnose_categories` is checked before `triage_only_categories`
     before the built-in `FIX` set.

7. **R7.6 stale-closed re-check** (`runner/executor.py:996-1008`):
   immediately before cloning/running the LLM loop on the `FIX` path, the
   executor re-reads the issue's work row; if it was closed with
   `closed_reason=="issue_closed"` while the task sat in the queue, the run
   aborts with zero cost — no clone, no agent call, no comment.

8. **`issue_work` open/resume/reopen** — `_open_or_resume_issue_work()`
   (`runner/executor.py:676-761`), run right after R7.6 passes. Three
   distinct outcomes depending on the existing row for this issue:
   - **No row, or closed with `no_changes` / `max_attempts` /
     `terminal_failure`**: treated as a spent work item — deleted (if
     present) and a brand-new row opened with a freshly computed branch
     name. The workspace clones from `base_branch` (`executor.py:1041`) and
     checks out a new local branch from that clone's tip
     (`executor.py:1085`); no prior `last_summary` is injected into the
     prompt.
   - **Row still `open`** (a genuine in-flight resume, e.g. after an
     orphan-task restart): `attempts` is bumped, the workspace fetches and
     checks out the existing remote branch's tip when the WIP push
     succeeded (`_remote_branch_exists`, `executor.py:1065-1085`), and
     `last_summary` rides into the next prompt as `resume_summary`
     (`executor.py:1020-1026`) — both gated on `resume.enabled`.
   - **Closed with `pr_opened`** (or `branch_published`, its counterpart on
     a forge with no PR API): reopened *in place* (not deleted) —
     `work_branch`, `pr_number`, `total_cost_usd`/`total_iterations`, and
     `last_summary` all carry over unchanged, and `attempts` is
     deliberately **not** bumped (that counter is a block-retry budget,
     orthogonal to a human commenting on an already-successful issue). The
     workspace fetches and checks out the *existing* PR branch's tip
     instead of cloning fresh, and the last run's summary is injected as
     `resume_summary` — both **regardless of `resume.enabled`**, via a
     `force_continue` flag this reopen case sets specifically because it's
     a distinct continuity feature the block-retry toggle shouldn't gate
     (`executor.py:1020-1026,1066`). The executor
     then finds the still-open PR by branch name
     (`PullRequestClient.find_open_pr`) and updates it instead of opening a
     duplicate. On a forge with no PR API there is no PR to find: the
     follow-up pushes onto the same branch and reports on the issue with
     the compare link again.

## Fires when

| Condition | Result | Reference |
|---|---|---|
| Verified webhook (`issues`/`issue_comment`) on a configured instance path, non-duplicate delivery, repo in `config.yaml`'s `repos[].issues` | Passes HTTP layer | `routes.py::handle_webhook`; `providers/github/webhook.py` |
| `issues.opened` | Task enqueued (`ISSUE_OPENED`) regardless of labels; classification happens later at worker time | `providers/github/webhook.py`; `worker.py` |
| `issues.labeled` where the issue's *full current* label set has a category label or `agent-skip` | Task enqueued (`LABEL_ADDED`); worker classifies using that set, not just the label just added — an unrelated label added to an issue already labeled `bug` still enqueues | `label_router.py::labels_can_trigger`; `routes.py` |
| `issue_comment.created` on a **genuine issue** (not a PR), which is **open**, from an author who is not fly-swe's own bot, and who is the issue's author OR has an allowed `author_association` OR is on the explicit allowlist | Task enqueued (`COMMENT_COMMAND`); no comment-body filtering | `comment_filter.py`; `policy/ingestion.py` |
| Task passes all worker guards and classifies as `FIX` (`bug`, `docs`, `chore`, `agent-fix-please`, or low/no-severity `bug`, by default config) | The coding agent actually runs (clone, LLM loop, PR) | `label_router.py`; `worker.py` (FIX branch) |
| Task passes all worker guards and classifies as `DIAGNOSE` (a category listed in `policy.diagnose_categories`, empty by default) | The coding agent runs read-only (clone, LLM loop, no edits) and posts a verdict comment; no PR | `label_router.py`; `runner/diagnose.py` |

## Does NOT fire when

| Condition | Result | Reference |
|---|---|---|
| POST to a path whose instance isn't configured (e.g. `/webhooks/gitlab`) | HTTP 404 `unknown provider instance`, nothing recorded | `routes.py::handle_webhook`; `providers/registry.py` |
| Event type other than `issues`/`issue_comment` (e.g. `pull_request`, `push`, `check_suite`, `ping`), validly signed | `200`, `reason: unsupported_event`, no task | `providers/github/webhook.py` |
| Missing/invalid HMAC signature, empty `PROVIDERS__<ID>__WEBHOOK_SECRET`, or missing `X-GitHub-Delivery` header | HTTP 400, delivery not recorded | `GitHubWebhookAdapter.verify_signature` / `delivery_key` |
| Same delivery key already processed | `200 {"status":"duplicate"}`, no reprocessing | `routes.py`; `store.py::is_delivery_processed` |
| Repo not in `config.yaml`'s `repos:` list | Dropped, `reason: repo_not_allowed` | `routes.py` |
| Unhandled action on an allowed event (`issues.assigned`, `issues.edited`, `issues.deleted`, etc.) | Dropped, `reason: unhandled_event_action` | `providers/github/webhook.py` |
| `issues.closed` / `issues.reopened` | Handled inline, **no Task enqueued**, zero cost either way | `routes.py::_handle_issue_lifecycle` |
| `issue_comment.created` on a **PR's Conversation tab** rather than a genuine issue (`issue.pull_request` key present) | Dropped, `reason: comment_on_pull_request`, before any other comment filter runs | `policy/ingestion.py` |
| Comment authored by fly-swe's own bot account | Dropped, `reason: own_bot_comment` | `policy/ingestion.py` (`Author.is_self`) |
| Comment on a closed issue | Dropped, `reason: issue_closed` | `policy/ingestion.py` (`IssueRef.state`) |
| Comment author is not the issue author, not in `allow_associations` (default `OWNER`/`MEMBER`/`COLLABORATOR`), and not on the explicit allowlist | Silently dropped, `reason: comment_author_not_allowed` — no task, no reply, no cost | `comment_filter.py`; `policy/ingestion.py` |
| Same, but the payload reported no association (`unknown`) | Task enqueued, then `ABORTED_POLICY` (`comment_author_not_allowed`) by the worker before any LLM call; no reply | `guards.py::check_comment_author_allowed`; `worker.py` |
| `issues.labeled` where the current label set has no category label and no `agent-skip` (e.g. only `wontfix`) | Dropped, `reason: no_trigger_label` — no task, no comment. Before v0.10.0 this enqueued a task that aborted and posted a "blocked" comment | `label_router.py::labels_can_trigger`; `policy/ingestion.py` |
| `issues.reopened` (always — this is a lifecycle event, never a task-creating trigger) | Handled inline: if the local `issue_work` row's `closed_reason=="issue_closed"`, the row is deleted so the *next* trigger starts clean; for any other `closed_reason` the row is left untouched — the reopen event itself never enqueues a Task either way. A *later* comment/label-added event still enqueues a Task normally, and what that Task's executor run does with the row then depends on `closed_reason`: fresh start for `no_changes`/`max_attempts`/`terminal_failure`, in-place continuation for `pr_opened` | `routes.py:59-80`; see [Known gaps](#known-gaps-in-the-current-implementation) item 3 |
| Label classification resolves to `SKIP` (`agent-skip` present, or an `issues.opened` with no recognized label) | `ABORTED_POLICY`, a "blocked" comment is posted, no LLM cost | `label_router.py`; `worker.py` (SKIP branch) |
| Label classification resolves to `TRIAGE_ONLY` (default: `question`, `feature-request`, high/critical `bug` — configurable via `policy.triage_only_categories`) | A templated comment is posted; the coding agent never runs | `worker.py` (TRIAGE_ONLY branch); `triage/builder.py` |
| Label classification resolves to `DIAGNOSE` (a category listed in `policy.diagnose_categories`, empty by default) | The coding agent runs read-only and posts a verdict comment; it never edits files, commits, or opens a PR | `runner/diagnose.py` |
| `validate_task_payload`, `check_repo_allowed`, or (if configured) `check_author_allowed` fails at worker time | `FAILED_TERMINAL` / `ABORTED_POLICY`, "blocked" comment posted, agent never runs | `guards.py:13-53`; `worker.py:411-457` |
| Daily budget for the task's target repo is at/above `budgets.max_daily_cost_usd` | Task requeued (not aborted); no comment spam per poll cycle; retried automatically once budget allows | `guards.py:56-66`; `worker.py:459-507` |
| Issue was closed while the `FIX` task sat in the queue (R7.6) | Executor aborts before clone/LLM call, zero cost, no comment | `executor.py:996-1008` |
| Orphaned/failed task exceeds `budgets.max_task_retries` (3) | `FAILED_TERMINAL`, no further retries; if the work row is still open the comment names the saved branch and says a new comment resumes it | `worker.py` (`run_retry_recovery`) |
| The LLM provider fails mid-run (`llm_error`: connection/SSL drop, timeout, 5xx, 429 after the retry layers) | WIP committed and pushed to the work branch, `issue_work` stays open, task `FAILED_RECOVERABLE` → re-queued automatically with backoff (`budgets.max_task_retries`); a new comment also resumes at once | `executor.py` (`_finish_salvaged_failure`) |
| Any other agent crash / `format_error` | WIP pushed, `issue_work` stays open, task `FAILED_TERMINAL` (no auto-retry); a new comment resumes from the branch | `executor.py` (`_finish_salvaged_failure`) |

## What the live `config.yaml` actually controls

The reference deployment's `config.yaml` (reproduced below, with the App id
and repo names replaced by placeholders) has **no `comments:` block and
no `resume:` block**. `config.yaml.example` documents both, but since they're
absent from the live file, the comment-author filter and resume behavior run
entirely on hard-coded Python defaults in `src/issue_agent/config.py:471-527`
(`comments.author_filter.enabled=true`, `allow_issue_author=true`,
`allow_associations=["OWNER","MEMBER","COLLABORATOR"]`, `allowlist=[]`) rather
than an explicit, reviewable value in the file that's actually deployed.

```yaml
agent:
  name: issue-agent

providers:
  github:
    type: github
    app_id: 123456

repos:
  - issues: example-org/issues
    target: example-org/app
    base_branch: dev
    workflow: gitflow
    branch_prefix: agent
    draft: false
    labels: []
    project:
      test_command: ""
      lint_command: ""
      relevant_extensions: []
      protected_paths: []

llm:
  model: openrouter/deepseek/deepseek-v4-flash
  # ... provider/base_url/retries/cost_tracking/temperature/fallback

budgets:
  max_iterations: 40
  max_cost_per_task_usd: 0.50
  max_daily_cost_usd: 5.00
  task_timeout_seconds: 900
  command_timeout_seconds: 120
  max_task_retries: 3

worker:
  poll_interval_seconds: 2.0
  orphan_timeout_seconds: 90
  backoff_schedule_seconds: [60, 300, 1800]

pr:
  slug_max_length: 30
  label: agent-generated

# git_identity, metrics, prompts.category_map, retention also set
```

None of `repos:`, `budgets:`, `worker:`, `pr:`, etc. affect *whether* the
agent fires — they affect *how* it behaves once triggered (which repo, what
branch, how much it's allowed to spend, how prompts are chosen). The only
config-driven trigger gate that exists today is the repo allowlist
(`repos[].issues`) and the implicit comment-author defaults described above.

## Known gaps in the current implementation

> **Note on `pull_request_review`:** fly-swe previously scaffolded a
> `pull_request_review` trigger (`TriggerKind.REVIEW_CHANGES_REQUESTED`),
> intended for a future loop where the agent reacts to a human requesting
> changes on a PR it opened itself. It was never wired to any handler
> (`label_router.py`/`worker.py`/`executor.py` had zero references to it)
> and has since been removed from the codebase — the event is now rejected
> like any other unsupported event type (acked `200 ignored`, never acted
> on). A real PR-review integration is
> planned for when the agent is evolved to work on PR reviews directly;
> this document no longer describes it as a "known gap" because it no
> longer exists in the codebase to have a gap.

1. **`TriggerKind.COMMENT_COMMAND` is a misnomer.** No comment-body or
   slash-command parsing exists anywhere in the codebase; the trigger fires
   on *any* comment text from an allowed author.
2. **Label classification is deferred to worker time, inconsistently with
   the comment filter.** Concretely: a comment from a disallowed author is
   dropped *at the webhook layer* — no `Task` row is ever inserted, no
   reply is posted, zero trace (`routes.py:226-241`). But an issue opened
   with no recognized label (or a label like `enhancement` that isn't in
   `label_router.py`'s categories) **does** get a `Task` row inserted
   immediately at webhook time (`routes.py:243-296`), because the
   `issues.opened`/`issues.labeled` handler never looks at what the labels
   actually are — it always enqueues. Only when the worker later dequeues
   that task (up to `poll_interval_seconds` later) does `classify_labels()`
   inspect the labels, find nothing recognized, decide `SKIP`, and post a
   visible `🤖 … task not processed` comment on the issue
   (`worker.py:511-536`). Two scenarios that both mean "this isn't agent
   work" produce opposite observable outcomes: one is silent, the other
   creates a DB row, a worker cycle, and a bot comment on every unlabeled
   or mis-labeled issue. *Partially addressed in v0.10.0:* an
   `issues.labeled` event with no trigger label in the set is now dropped
   at the webhook (`reason: no_trigger_label`); `issues.opened` without a
   recognized label still enqueues and posts the `SKIP` comment.
> **Note on rework/token waste on re-triggers:** this document previously
> flagged that *any* re-trigger on a closed issue started completely from
> scratch, with no record of what happened last time — wasting tokens
> re-deriving the same conclusion, and for `pr_opened` specifically,
> discarding the existing PR's diff via a force-push over a fresh clone.
> This has since been addressed for the two outcomes where it mattered
> most: `no_changes` and `pr_opened` closes now persist a `last_summary`
> (`last_summary_kind="submission"`, the agent's own final report —
> `_resolve_agent_summary()`, `executor.py:473-482`; persisted at
> `executor.py:854-860` for the block/zero-WIP `no_changes` case,
> `executor.py:1226-1229` for the completed/clean-tree `no_changes` case
> — also surfaced in the posted comment, `executor.py:549-552`
> — and `executor.py:1355` for `pr_opened`), and a
> `pr_opened` re-trigger reopens its `issue_work` row *in place* and
> continues from the existing PR branch instead of re-cloning
> (`_open_or_resume_issue_work`, pipeline stage 8 above) — regardless of
> `resume.enabled`. `no_changes`, `max_attempts`, and `terminal_failure`
> deliberately keep the fresh-start behavior described below; only
> `pr_opened` gets the continuity treatment.

3. **Reopening an issue is a silent no-op except for one specific closure
   reason — and, since the note above, `pr_opened` is no longer even in
   that category.** Two layers are easy to conflate here:
   - *GitHub's side*: manually closing via the UI, closing via the API, and
     auto-closing through a merged PR with a `Fixes #N`/`Closes #N`
     keyword all produce the exact same `issues` webhook
     (`action: "closed"`) — the code has no way to (and doesn't try to)
     tell these apart. GitHub also sends a `state_reason` field
     (`"completed"` vs `"not_planned"`) distinguishing "resolved" from
     "won't fix" closures, but **this codebase never reads it**
     (`grep` for `state_reason` across `src/` returns nothing) — every
     closure is treated identically regardless of why the issue was
     closed.
   - *fly-swe's own bookkeeping*: the `closed_reason` column on an
     `issue_work` row is a separate, internal concept — "why did the
     agent's local record of this issue's work stop being open" — with 6
     distinct values, only one of which maps to an actual GitHub closure:

     | `closed_reason` | Meaning | Set at |
     |---|---|---|
     | `issue_closed` | The GitHub issue was actually closed (any of the methods above), or a periodic reconciliation sweep caught a missed webhook | `routes.py:84`; `observability/cleanup.py:109` |
     | `pr_opened` | Happy path: the agent succeeded and opened a PR. Also persists `last_summary` (the PR description) — see the note above; the only `closed_reason` a later re-trigger reopens *in place* rather than starting fresh | `runner/executor.py:1357` |
     | `branch_published` | Happy path on a forge with no PR API (PRD §3.7): the branch was pushed and the issue got a compare link instead of a PR; `pr_number` stays empty. Persists `last_summary` and is reopened *in place* on a re-trigger, exactly like `pr_opened` | `runner/executor.py` (`_close_issue_work(..., out.delivery_reason)`) |
     | `no_changes` | The agent ran but produced a clean diff (nothing to change), or blocked with no WIP worth preserving. Also persists `last_summary` since the note above | `runner/executor.py:861,1231` |
     | `max_attempts` | The agent hit a recoverable block (timeout/budget) repeatedly and exhausted its resume-retry ceiling (`resume.max_attempts`, default 3) | `runner/executor.py:893` |
     | `terminal_failure` | An unrecoverable error (e.g. a git-level failure), or a failed run with `resume.enabled: false` (nothing preserved). **Not** used for LLM/agent failures when resume is on: those push the WIP and leave the row open (see [operations.md § Failure handling](./operations.md)) | `runner/executor.py` (`_close_issue_work(..., "terminal_failure")`) |

     The R7.6 stale-closed guard (`executor.py:1001`) is the *only* place
     that re-checks `closed_reason`, and it only reacts to `issue_closed`.
     So `issues.reopened` clearing the row (`routes.py:59-80`) only matters
     for that one value; for the other five, the row is simply left as-is —
     reopening the GitHub issue changes nothing by itself. This doesn't
     permanently block future work, though: a **new** comment or label
     still creates a fresh task normally regardless of a stale
     non-`issue_closed` `closed_reason`, since nothing else in the pipeline
     checks that field. It's specifically the *reopen action* that's a
     no-op outside the `issue_closed` case — easy for a maintainer to
     misread as "the agent won't touch this issue again." What that
     follow-up task then *does* once dispatched depends on `closed_reason`
     too: for `pr_opened` and `branch_published` it's the in-place reopen described in pipeline
     stage 8 (existing branch, existing PR, `last_summary` carried over);
     for `no_changes`/`max_attempts`/`terminal_failure` it's still a fresh
     start with no continuity, by design — none of those states have a
     surviving branch/PR worth continuing from.

## Recommendations

### Config-level (`config.yaml` schema/knobs)

- **Make implicit defaults explicit.** Add the `comments:` and `resume:`
  blocks to the live `config.yaml` (even if just restating the current
  defaults) so the deployed comment-author policy is reviewable in the file
  that's actually shipped, rather than requiring a read of
  `config.py`'s Python defaults to know what's enforced in production.
- **Add a trigger-label allow/deny list.** Currently *any* label change
  enqueues a task that's classified only after guards run. A
  `labels.trigger_on` / `labels.ignore` config knob would let operators
  restrict which labels even create a DB row, reducing noise and aligning
  webhook-time behavior with the comment filter's silent-drop model.

### Implementation-level

- ~~Check for `payload["issue"]["pull_request"]` in the `COMMENT_COMMAND`
  path so isolation from PR comments doesn't depend on an accident of the
  split-repo topology.~~ **Done** — the webhook adapter marks such events
  `target_kind="pr"` and the core ingestion filter drops them unconditionally, independent of whether
  `repos[].issues` and `repos[].target` are the same repo.
- ~~Persist a closing summary for `no_changes`/`pr_opened`, and make a
  `pr_opened` re-trigger continue from the existing branch instead of
  re-cloning from scratch, to cut wasted tokens/rework on follow-up
  comments.~~ **Done** — see the note above item 3 in Known gaps.
- **Read GitHub's `state_reason` field** on `issues.closed` webhooks and
  consider surfacing it (e.g. in the audit log or a metric label) so
  "completed" and "not_planned" closures are at least distinguishable after
  the fact, even if they're handled identically today.
- **Move label classification earlier**, at webhook-ingestion time, so an
  issue with no recognized label (or an explicit `agent-skip`) is dropped
  the same way a disallowed comment author is — no Task row, no "blocked"
  comment cycle — for consistency between the two filtering layers.
- **Surface the reopen no-op.** When `issues.reopened` fires on a row whose
  `closed_reason` is `no_changes`, `max_attempts`, or `terminal_failure`
  (not `pr_opened`, which now genuinely continues on the next trigger —
  see above), post a short comment or emit a clearly-labeled log/metric
  explaining that reopening the issue alone won't restart anything, so the
  behavior isn't mistaken for a bug by whoever reopened it.

> **Note on resumed branches (issue #86):** on a `pr_opened` reopen / resume the
> harness merges the current `origin/<base>` into the work branch before the
> agent runs (`_update_branch_with_base`; a conflicting merge is aborted and the
> branch left as is). The self-commit salvage resets to the branch tip the agent
> started from, not `origin/<base>`, and every push is preceded by
> `_assert_branch_integrity`, which aborts (`branch_integrity`) rather than push a
> tip whose tree reverts content already on the base.
