# PRD — Provider Plugins Architecture

**Status:** Implemented. Phases 0–4 done (v1.1.0; see §1.1).
Revised on 2026-10-24 with the generic prerequisites for forges with no PR API.
**Date:** 2026-10-19 (revised on 2026-10-24; status as of 2026-10-31, v1.1.10)
**Type:** `feat` (architectural refactor with a configuration change)

## Revision history

| Date | Change |
|---|---|
| 2026-10-31 | Legacy cleanup (v1.1.10): the trigger backfill (`webhook/backfill.py`, the last core module importing the GitHub plugin) is removed, the webhook no longer stores the raw payload, and migration v8 drops `tasks.payload_json`/`installation_id` (§3.6); `runner/delegators/` removed (§2.6); `tests/unit/test_architecture.py` guards the core against forge imports. |
| 2026-10-28 | Phase 4 done (v1.1.0): §1.1 updated; prompts and `INSTANCE_TEMPLATE` free of forge vocabulary (`terms.*` and `instructions_files` in `runner/prompts.py::prompt_vars`); `provider_instance` in metrics and audit; health/readiness already iterated the instances since Phase 3; "Provider plugins" section in `AGENTS.md`. |
| 2026-10-27 | Phase 3 done (v1.0.0): §1.1 updated; implementation addendum in §3.5/§3.6 (migration v7 instead of "v6b", `from_settings` with the instance config and the root `Settings`, `AGENT_NAME`, `default_commit_email`, legacy sections and secrets in the YAML rejected, rule P4 documented); the semver decision in §8 settled on 1.0.0. |
| 2026-10-26 | Phase 2b done, part 4 of 4 (v0.13.0): flow with no PR API in the executor; §1.1 updated; implementation addendum in §3.2 (`branch_published`, comment with a compare link, labels on the issue, local conflict via `git merge-tree` with a fetch of the base, fallback when the forge doesn't respond, `terms.pr` vocabulary only in the new text). |
| 2026-10-26 | Phase 2b, part 3 of 4 (v0.12.2): executor on top of the plugin; `get_shared_auth` and `PRService` removed; §1.1 updated; implementation addendum in §3.2 (per-task `_Outbound`, bodies cut at `max_body_length`, comment history fetched in `run_task`, draft via title prefix, forge with no PR API refused until part 4). |
| 2026-10-26 | Phase 2b, part 2 of 4 (v0.12.1): worker, diagnose, workspace, reconciler and `/readyz` on top of the plugin; §1.1 updated; implementation addendum in §3.2 (`ProviderRegistry` created in `worker.main()`, comments through the task's instance, `Workspace` on top of `GitTransport`, `author_permission` in the `unknown` guard, per-instance `/readyz` keys). |
| 2026-10-25 | Phase 2b, part 1 of 4 (v0.12.0): outbound ports and GitHub adapters; §1.1 updated; implementation addendum in §3.2 (clients bound to a repo, per-repo credential inside the plugin, `pr_has_conflicts` with the pushed SHA, `format_ref` with `repo`/`from_repo`, `ProviderHealth`, `supports_pr_api` × `pulls()` coherence in the registry, PR text in `core/pr_text.py`). |
| 2026-10-25 | Phase 2a done (v0.11.0): §1.1 updated; implementation addendum in §3.2 (inbound ports only, `ParsedDelivery`/`WebhookRejectedError`, ingestion filters in the core, `IssueRef.state`, `from_settings` on top of the root `Settings`, no network in the handler). |
| 2026-10-25 | Phase 1 done (v0.10.0): §1.1 updated; implementation addendum in §3.1 (`TriggerEvent.actor`, `Ignored(reason, repo)` instead of `None`, optional `Comment.created_at`, `unknown` guard in the worker, `no_trigger_label` filter). |
| 2026-10-24 | Readiness review for a new provider: new §1.1 (implementation status per phase); §2 rechecked against `main` v0.9.13 (lines and counts updated; §2.6 items marked as fixed in Phase 0); **generic prerequisites** the ports did not yet cover: forge with no PR API as a first-class flow in the core (§3.7), label semantics without a diff (§3.1), author association unavailable in the webhook (§3.1/§3.2), `FakeProvider` with no PR API in the contract suite (§5); Appendix A corrected (Gogs). The Gogs implementation now has its own PRD: [`PRD-gogs.md`](./PRD-gogs.md). |
| 2026-10-19 | Revision: assumptions validated against the code (§2 corrected and expanded), interface split into cohesive ports (§3.2), **provider instances** (N per type) in the config and in the schema (§3.3, §3.5, §3.6), Phase 0 for fixing latent bugs, Phase 2 split into 2a/2b, prior art and libraries evaluated (Appendix A). Owner's decisions: instances now; **hard break** of config/env (no deprecation aliases); Gogs kept on the roadmap with optional `supports_pr_api`. |
| 2026-10-19 | Original version. |

---

## 1. Vision and goals

Today fly-swe is a monolithic service coupled to GitHub: GitHub webhooks
come in, become tasks, and the result goes out as a Pull Request on GitHub. The goal
of this PRD is to turn the support for **GitHub issues** into a **provider
plugin**, so that new providers (GitHub Pull Requests as a trigger,
GitLab, Gitea/Forgejo, Gogs) can be added later **without touching the service
core**.

### Goals

1. Define a `ProviderPlugin` interface in the core, following the pattern already
   established by `ToolchainPlugin` (`src/issue_agent/runner/toolchains/`) —
   but **without** repeating the `ToolchainManager`'s if-by-name (see §3.3).
2. Move all GitHub-specific code to `src/issue_agent/providers/github/`,
   registered as the first plugin.
3. Introduce a **canonical domain model** in the core (Anti-Corruption Layer),
   eliminating the leak of the raw GitHub webhook payload into generic
   modules. The starting point is the `ParsedWebhook` that already exists in
   `webhook/parser.py` (today with fields nobody consumes).
4. Generalize configuration, authentication and persistence to support
   **provider instances** — several of the same type (github.com + GHE,
   gitlab.com + self-hosted GitLab) — with a static registry of built-in
   *types* and config typed by a discriminated union.

### Architectural success criterion

> Adding a GitLab provider must require only: creating
> `src/issue_agent/providers/gitlab/` implementing `ProviderPlugin`, registering
> the type in the static registry and adding an instance to the config. **Zero
> changes to `queue/`, `worker.py`, `runner/`, `policy/` or `observability/`**
> and **no `owner/repo` split in the core** (the `repo` is an opaque slug).

### Non-goals

- **Implementing** the GitHub PR plugins (trigger by review/comment on a
  PR), GitLab, Gitea/Forgejo or Gogs — they come in only as acceptance
  criteria for the architecture and future roadmap (§9).
- Plugin discovery via `entry_points` / pluggy / stevedore / external
  packages — the registry is static and built-in.
- Multi-tenancy beyond what already exists (one service instance; one set of
  credentials per provider instance).
- Changes to the agent execution contract (the "Agent Execution Contract" in
  `docs/operations.md` stays: the agent edits files, the harness does git).
- **Compatibility with old env/YAML names.** Explicit decision: hard
  break in Phase 3, with no deprecation aliases (§3.5, §8).

### 1.1 Implementation status (2026-10-24, `main` v0.9.13; Phases 1–4 updated on 2026-10-25/28, v1.1.0)

| Phase | Status | Evidence |
|---|---|---|
| 0 — Latent bugs | ✅ Done | GHE honors `host` in `git_auth_env` (now with no default), `base_url` in `GithubIntegration`/`Github` and the PR URL; an invalid signature does not burn the delivery (`routes.py:125-134`); `litellm` declared in `pyproject.toml`; `github_installation_id` removed; follow-up characterization tests in `tests/integration/test_executor_follow_up.py` |
| 1 — Canonical model | ✅ Done | `core/models.py` (`Author`, `Comment`, `IssueRef`, `TriggerEvent`); the GitHub parser normalizes (today `providers/github/webhook.py`) and the ingestion filters run on the canonical model (today `policy/ingestion.py`); migration v6 + `webhook/backfill.py` (both the backfill and the `payload_json` column were removed in v1.1.10, migration v8); no consumer outside `webhook/`/`github/` reads `payload_json`; `list_issue_comments` returns `Comment`; `comment_filter` on `Author`, with the `unknown` path in the worker's `check_comment_author_allowed` guard; `label_added` without a trigger label discarded with `no_trigger_label` |
| 2a — Ports, registry and webhooks | ✅ Done (v0.11.0) | `providers/base.py` (`ProviderCapabilities`, `WebhookAdapter`, `ProviderPlugin`); `providers/registry.py` in `app.state.registry`; `providers/github/webhook.py` absorbed `webhook/parser.py` and `webhook/validation.py` (HMAC fail-closed with an empty secret); dispatcher `POST /webhooks/{instance_id}` with no GitHub header and no network in the handler; ingestion filters in the core (`policy/ingestion.py`); `tests/fakes/fake_provider.py` (with and without association) and `tests/unit/providers/test_provider_contract.py` |
| 2b — `ForgeClient`/`GitTransport` | ✅ Done (v0.13.0) | Done: outbound ports in `providers/base.py`; `providers/github/` with `auth.py`, `client.py` (`GitHubForgeClient`), `pr.py` (`GitHubPullRequestClient`), `transport.py` (`GitHubTransport`) and `normalize.py`; PR text in `core/pr_text.py`; `FakeProvider` with and without a PR API; the contract suite covers the outbound ports; worker, diagnose, workspace, reconciler, `/readyz` and `runner/executor.py` on top of the plugin; `get_shared_auth` and `PRService` removed; outside `providers/`, no GitHub import; a forge with no PR API delivers the branch with `closed_reason="branch_published"` (`tests/integration/test_executor_branch_published.py`) |
| 3 — Per-instance config | ✅ Done (v1.0.0) | `Settings.providers` (discriminated union in `providers/settings.py`, `GitHubProviderSettings` in `providers/github/settings.py`) with `env_nested_delimiter="__"`; `github_*` fields and `github:`/`github_app:` sections removed (legacy sections and secrets in the YAML fail at load time); `repos[].provider` and `route_for(instance, repo)`; `ProviderRegistry.from_settings` iterates the instances; migration v7 (`processed_deliveries` and `issue_work` with `provider_instance` in the PK, pending index per instance); per-instance reconciler; `installation_id` is no longer written or read outside the migrations; migration guide and rule P4 in `docs/operations.md`; `tests/integration/test_webhook_two_instances.py` |
| 4 — Cleanup | ✅ Done (v1.1.0) | `prompts/*.md` and `INSTANCE_TEMPLATE` render `{{ terms.issue }}`, `{{ terms.pr }}`, `{{ terms.ref_prefix_pr }}` and `{{ instructions_files }}` (`runner/prompts.py::prompt_vars`, passed via `build_agent(terms=...)`); `build_user_message`/`build_branch_status` use `terms`; a test fails on raw "GitHub"/"Pull Request"/"PR"/"issue" in the prompts and renders everything with the `FakeProvider` vocabulary; `provider_instance` in every metric that has `repo` and in the task/`issue_work` audit entries; `/readyz` already iterated `registry.all()` (Phase 3); `AGENTS.md` with the "Provider plugins" section |

**Verdict:** with Phases 0–4 done, the core **is ready** for a new
provider: a plugin comes in as a package in `providers/<type>/`, a settings
model in the union in `providers/settings.py` and an entry in
`BUILTIN_PROVIDER_TYPES`, without touching `webhook/routes.py`, `worker.py`,
`runner/executor.py`, `policy/`, prompts or schema. The next one is Gogs
([`PRD-gogs.md`](./PRD-gogs.md)).

---

## 2. Context: current couplings to GitHub

Validated against the code on 2026-10-19 and rechecked on 2026-10-24 against
`main` v0.9.13 (≈10.4k LOC in `src/`, ≈22.4k LOC and 1,270 tests in `tests/`). The points below are what the refactor needs
to resolve.

### 2.1 Structural — raw payload leaks into the core

`queue/models.py::Task.payload_json` (`models.py:46`) carries the raw GitHub
webhook payload (`json.dumps(payload)` in `webhook/routes.py:265,281`), which is
**re-parsed in at least 9 places**, all assuming GitHub's shape
(`issue.number`, `issue.labels[].name`, `repository.full_name`, `sender.login`,
`comment.author_association`, `comment.performed_via_github_app`):

- `webhook/parser.py` — `parse_webhook_payload`, `_infer_trigger_kind`,
  `comment_is_from_own_app`, `issue_is_closed`, `comment_is_on_pull_request`
  (**it is the main GitHub parser; it was missing from the previous version of the PRD**)
- `webhook/routes.py:246-266` — reads `comment.user.login`,
  `comment.author_association`, `issue.user.login` straight from the payload, and
  re-parses the freshly serialized payload just to extract labels (which
  `parsed.labels` already has)
- `policy/guards.py` — `validate_task_payload`, `check_author_allowed`
- `policy/label_router.py` — `classify()`, `extract_labels_from_payload()`,
  `_extract_labels_from_payload`/`_extract_labels_from_dict`
- `runner/prompts.py` — `_extract_issue_info`, `build_comments_block`
- `runner/executor.py` — `_extract_issue_title`, `_extract_author_login`
- `triage/builder.py::_extract_issue_info`
- `diagnose/builder.py::_extract_issue_info` (via `runner/diagnose.py`)

Besides `payload_json`, the GitHub shape also leaks through normalized dicts from
`github/client.py::list_issue_comments` (with `author_association` and
`performed_via_github_app_id`), consumed by `runner/prompts.py` and by
`parser.comment_dict_is_own_app`.

Note: `ParsedWebhook.labels/sender/issue_title/issue_body/raw` are **never
read** in `src/` — the embryo of the canonical model exists, but the core ignores it.
This is the most invasive refactor and the prerequisite for any other provider.
Test footprint: 21 files build payloads in the GitHub shape (largest:
`test_guards.py`, `test_label_router.py`, `test_worker.py`, `test_executor.py`).

### 2.2 Provider credential crosses the domain

`installation_id` (a GitHub App concept) is in `Task` (`models.py:48`), in the
schema (`tasks.installation_id INTEGER NOT NULL DEFAULT 0`) and has **5**
network/fallback resolution points:

| Location | Resolves from |
|---|---|
| `webhook/routes.py:269-273` (network on the hot path, blocking, on the event loop) | **issues** repo |
| `worker.py:240-254` (`_resolve_installation_id`) | **target** repo |
| `runner/executor.py:1916-1918` | **target** repo |
| `runner/diagnose.py:87-90` | **target** repo |
| `observability/cleanup.py:66-69` | **issues** repo (cached per sweep) |

In addition, `runner/executor.py:176-179` (`_fetch_comment_history`) uses
`task.installation_id` with no fallback and silently returns `[]` when it is 0.

**Latent inconsistency:** ingestion resolves the installation from the issues repo
and the worker from the target repo, and a single id is used for both (comments on
`task.repo`, clone/push/PR on `target_repo`). It only works if a single
installation covers both repos. The new design resolves credentials **per repo**
(§3.2). The `github_installation_id` field (dead config) was removed in Phase 0.

### 2.3 Global singletons assume a single GitHub App

`github/auth_provider.py::get_shared_auth()` (module-global `_shared_auth`) and
`config.py::get_settings()` (`lru_cache`) assume a single App per process.
Real scale of the problem in the tests: **121** references to `get_shared_auth` in 10
files (the autouse fixture `_clear_shared_auth` in `tests/integration/conftest.py`
only masks it), 58 `GITHUB_APP_ID=` kwargs in 24 files, 117 occurrences of
`installation_id` in 18 files. **There is no provider fake**: everything is
`unittest.mock`.

### 2.4 GitHub quirks embedded in generic code

| Quirk | Current location |
|---|---|
| PR URL in the GitHub format `{github_host}/{repo}/pull/{n}` (the host was fixed in Phase 0; the format is still GitHub's) | `runner/executor.py:1131` (`_build_fix_comment`) |
| Git credential format `x-access-token:<token>` (the `host` default was removed in Phase 0) | `runner/git_auth.py:30` |
| `author_association` (OWNER/MEMBER/COLLABORATOR) | `policy/comment_filter.py`, `runner/prompts.py`, `webhook/routes.py:250`, `github/client.py:58`, allowlist in `config.py` |
| Detection of the bot itself via `performed_via_github_app` | `webhook/parser.py:78-121`, `github/client.py:51,61`, `runner/prompts.py:240` |
| Bot identity (`github_app_bot_login` / `slugify(name)+"[bot]"`) | `webhook/parser.py:64-75`, `webhook/health.py:74-89` |
| Idempotency via the `X-GitHub-Delivery` header; event type via `X-GitHub-Event`; route `/webhooks/github` | `webhook/routes.py:103,110,120` |
| `processed_deliveries` with a PK on `delivery_id` only | `queue/schema.sql:1-6`, `queue/store.py:289-304` |
| HMAC SHA-256 — header `X-Hub-Signature-256` read in `routes.py:124`; `sha256=` prefix in `validation.py:28` | `webhook/routes.py`, `webhook/validation.py` |
| Auto-close via `Closes #N` / `owner/repo#N` | `github/pr.py:28-35,94` |
| Issues⊇PRs (`comment_is_on_pull_request`) | `webhook/parser.py:127-139` (called in `routes.py:213`) |
| Labels on the PR via the Issues API (`repo.get_issue(pr.number).add_to_labels`) | `github/pr.py:149` |
| `find_pr_by_branch` with the `owner:ref` filter (quirk) and `PRHeadMismatchError` | `github/client.py:66-83`, `github/pr.py:14` |
| Conflict check via GitHub's `mergeable`; text "GitHub still reports conflicts" | `github/pr.py:153-176`, `executor.py:1139,2521` |
| Clone URL `{github_host}/{owner}/{repo}.git`; `default_branch` read from the API | `runner/workspace.py:54`, `executor.py:1977` |
| "GitHub issue" / "Pull Request" vocabulary and `.github/copilot-instructions.md` in the prompts | `prompts/system.md`, `system_diagnose.md`, `bug_fix.md`, `doc_fix.md`, `runner/prompts.py:23,40,76` |
| Comment Markdown with `@login` and `#N` | `triage/builder.py`, `diagnose/builder.py`, `worker.py:257`, the executor's `_build_*_comment` family |
| Default git email `...@users.noreply.github.com`; `git_user_name` default `issue-agent[bot]` | `config.py:707,710` |
| Health check validates App ID / private key / webhook secret | `webhook/health.py:37-99` |
| `issue_work` reconciliation queries the GitHub API | `observability/cleanup.py:55-75` |
| `owner/repo` split on `/` | `routes.py:271`, `worker.py:253`, `executor.py:1917`, `diagnose.py:89`, `cleanup.py:68`, `workspace.py:33`, `client.py:78` |

### 2.5 Follow-up flow on an existing PR (not covered in the previous version)

The core makes many more calls to the provider than "create PR":
`executor._open_or_resume_issue_work` reopens `issue_work` in `pr_opened` when
a maintainer comments; it checks out the existing remote branch and the harness
merges `origin/<base>` (`_update_branch_with_base`, `_finish_base_merge`,
`_abandon_base_merge`); conflict state reaches the agent via
`prompts.build_branch_status` ("backs the open Pull Request #N"); the result is
posted on the PR via `comment_on_pr`; `pr_has_conflicts` polls
`mergeable`. The plugin interface (§3.2) needs to cover these operations —
`find_open_pr`, `comment_on_pr`, `pr_has_conflicts`, `get_default_branch` — and
not just `create_pr`.

### 2.6 Latent bugs — ✅ fixed in Phase 0

Historical record; the line references are the ones from before the fix.

1. ✅ **GitHub Enterprise was documented as supported
   (`README.md:191`, `config.yaml.example:35`) but did not work.**
   `git_auth_env(token, host=...)` never received `host=` (`workspace.py:94`,
   `executor.py:1589,1665,1939,1942,2337`), so the auth header only applied to
   github.com; `GithubIntegration`/`Github` did not receive `base_url`
   (`app_auth.py:21,44`); the PR URL was hardcoded (`executor.py:1092`).
   *Fixed:* `host` is now mandatory in `git_auth_env`, `base_url` comes from
   `Settings.github_api_url` and the PR URL uses `github_host`.
2. ✅ **An invalid signature marked the delivery as processed**
   (`webhook/routes.py:126`): a forged request with a real delivery-id would cause
   the legitimate delivery to be treated as a duplicate. *Fixed* in
   `routes.py:125-134`.
3. ✅ `litellm` was imported directly (`litellm_cost.py`, `llm_support.py`,
   `executor.py`), but only arrived as a transitive dependency of mini-swe-agent.
   *Fixed:* declared in `pyproject.toml`.
4. ✅ `github_installation_id` had no consumer in `src/`. *Removed.*
   (`runner/delegators/` also had no consumer in `src/`, only a test
   helper in `tests/helpers/fake_delegator.py`; *removed* in v1.1.10.)

### 2.7 Risk concentration

`runner/executor.py` has 2,601 LOC (~25% of `src/`) and mixes calls to
GitHub, git and agent orchestration. It is the biggest regression risk (Phase 2b).
`tests/unit/test_executor.py` has 4,700 LOC. The follow-up characterization tests
already exist (Phase 0, `tests/integration/test_executor_follow_up.py`).

---

## 3. Proposed architecture

Pattern: **Ports & Adapters (hexagonal)** with an **Anti-Corruption Layer** — the
canonical model (§3.1) is the ACL; each plugin is the adapter for one provider. The core
only knows the ports defined in §3.2.

### 3.1 Canonical domain model (core)

New module `src/issue_agent/core/` with **provider-independent** pydantic
models, produced by the plugin at ingestion. Starting point: evolve the
`ParsedWebhook` from `webhook/parser.py` (today with no consumers of its fields),
rather than creating a second parallel model.

```python
class Author(BaseModel):
    login: str
    association: Literal["owner", "member", "collaborator", "contributor", "none", "bot", "unknown"]
                             # "unknown": the forge's webhook does not report it (see rules below)
    is_self: bool            # the plugin flags whether it is the service's own bot

class Comment(BaseModel):
    id: str
    author: Author
    body: str
    created_at: datetime

class IssueRef(BaseModel):
    provider_instance: str   # id of the configured instance: "github", "gitlab-corp", ...
    repo: str                # OPAQUE provider slug; the core never splits on "/"
    number: int              # number visible in the API (GitHub number / GitLab iid)
    global_id: str | None    # provider's global id, when distinct from `number`
    title: str
    body: str
    labels: list[str]
    author: Author
    url: str

class TriggerEvent(BaseModel):
    kind: TriggerKind        # issue_opened | label_added | comment_command |
                             # issue_closed | issue_reopened (existing enum)
                             # label_added = "the issue's labels changed" (see rules)
    target_kind: Literal["issue", "pr"]   # issue_comment also fires on PR/MR
    issue: IssueRef
    trigger_comment: Comment | None       # only on comment_command; the history comes from list_comments
    delivery_key: str        # idempotency key computed by the plugin
    credentials_ref: str | None   # opaque; meaning defined by the plugin
```

Rules:

- **`raw_payload` is never parsed outside the plugin.** It is **not** persisted in
  `tasks`; if useful for debugging, it goes to `task_events`/audit. All extraction
  happens inside the plugin, during normalization.
- **Small events** (lesson from packit-service): the event carries the minimum
  needed to decide/enqueue; additional data (comment
  history, PR state) is fetched on demand by the `ForgeClient`.
- The **ingestion filters** currently scattered across `routes.py:209-263`
  (comment on a PR, comment from the bot itself, closed issue, author
  association) become the responsibility of `parse_event`, which returns
  `None` for ignored events.
- **Author association may be unknown at ingestion** (generic
  prerequisite, 2026-10-24 revision). Not every forge sends in the webhook something
  equivalent to `author_association`, and finding out the permission may require
  an API call with admin privilege. Rule: `parse_event` filters by
  association **only when it is known**. With `association="unknown"`, the
  event is enqueued and the worker applies `policy/comment_filter.py` before
  spending LLM. It first tries `ForgeClient.author_permission(login)` (§3.2,
  optional, cached per `(repo, login)`); if the plugin does not support it, it falls back to the
  login allowlist + issue author. The discard stays silent (no
  comment and no cost), and the webhook handler still has no network.
- **`label_added` semantics are independent of a diff** (generic prerequisite,
  2026-10-24 revision). Some forges only report "the labels changed" and send the
  current set, without saying which label was added. The core **already** classifies by the
  current set (`policy/label_router.py` reads `issue.labels`, not the added
  label), and the opened+labeled race is already deduplicated by
  `insert_task_unless_active_creation_task` (`store.py:341`). Rule: the plugin
  emits `label_added` whenever the label set changes and there is at least one
  label; `IssueRef.labels` is the current set; the decision to act belongs to the core
  (label router + `issue_work` state). This keeps `parse_event` stateless
  for all forges. The contract suite covers a "labels changed" event
  with no trigger label, which must become a no-op in the core.
- **Implementation addendum (Phase 1, 2026-10-25).** Differences from the
  sketch above, all already in `core/models.py` and `webhook/`:
  - `TriggerEvent.actor: Author` — who caused the event (GitHub: `sender`). On
    `labeled` it is whoever applied the label, not the issue author. It feeds the
    `allowed_authors` guard, the prompt's "Author:", the `Co-authored-by` and the
    triage/diagnose greetings. A plugin whose forge does not report the actor must fill it with the issue author.
  - `parse_event` returns `TriggerEvent | Ignored(reason, repo)` instead of
    `None`: the reason feeds the HTTP response and the logs, and the `repo` keeps
    `repo_not_allowed` taking precedence over the other filters. A malformed
    payload raises `ValidationError` (HTTP 400 `invalid_payload`).
  - `Comment.created_at: datetime | None` — the API may omit the date.
  - `parse_event(..., apply_filters=False)` re-normalizes without filters; used
    by the backfill of already-accepted tasks.
  - The `association="unknown"` path lives in
    `policy/guards.py::check_comment_author_allowed`, which since Phase 2b
    (part 2) resolves the association with `ForgeClient.author_permission`; if the
    lookup fails, it decides by issue author and allowlist only.
  - The no-op for `label_added` without a trigger label is a core filter in the
    handler (`label_router.labels_can_trigger`, `reason: no_trigger_label`);
    `agent-skip` counts as a trigger so the confirmation comment is kept.
- `queue/models.py::Task` now carries the serialized `TriggerEvent` in
  `trigger_json`, replacing the consumption of `payload_json` (§3.6).
- `installation_id` is replaced by `credentials_ref: str | None` — an opaque
  key (GitHub: serialized `installation_id`, **only as a hint**; the
  authoritative resolution is per repo, §3.2).
- `repo` is an opaque slug: GitLab has nested groups (`g/sub/proj`) and the core
  cannot assume `owner/name`. The `/` splits from §2.4 move to the
  plugin.

### 3.2 Plugin ports

The monolithic ABC from the previous version (one method per operation, with `creds` on
all of them) would scatter credential resolution and repeat the
issues-vs-target inconsistency from §2.2. The plugin now exposes **cohesive ports** (lesson from
Atlantis, which separates `EventParsing` from the VCS client):

```python
class ProviderCapabilities(BaseModel):
    draft_mode: Literal["native", "title_prefix", "none"]
    draft_prefix: str | None           # "Draft: " (GitLab), "WIP: " (Gitea/Forgejo)
    supports_pr_api: bool              # False (Gogs): fallback = push + comment with a compare link
    supports_labels: bool
    auto_close_keyword: str | None     # "Closes"; None = don't promise auto-close
    max_body_length: int               # truncation in the core (GitHub ~58,000; GitLab/Gitea ~1,000,000)
    terms: ProviderTerms               # vocabulary (below)

class ProviderTerms(BaseModel):
    issue: str                         # "issue"
    pr: str                            # "Pull Request" | "Merge Request"
    ref_prefix_issue: str              # "#"
    ref_prefix_pr: str                 # "#" | "!"
    repo_instructions_files: list[str] # e.g.: [".github/copilot-instructions.md"]

class WebhookAdapter(ABC):
    @abstractmethod
    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> None: ...
        # FAIL CLOSED: no secret configured => reject (Forgejo and Gogs send empty signatures).
    @abstractmethod
    def delivery_key(self, headers: Mapping[str, str], body: bytes) -> str: ...
        # GitHub: X-GitHub-Delivery. GitLab: webhook-id > Idempotency-Key > X-Gitlab-Event-UUID.
        # Gitea: may return a content-based key (event+object+updated_at) if the delivery id changes on redelivery.
    @abstractmethod
    def parse_event(self, headers: Mapping[str, str], body: bytes) -> TriggerEvent | None: ...
        # None = ignored (unsupported event, comment from the bot itself, closed issue, ...)

class ForgeClient(ABC):
    """API client already bound to a repo; renews credentials internally."""
    @abstractmethod
    def get_issue_state(self, issue: IssueRef) -> Literal["open", "closed"]: ...
    @abstractmethod
    def list_comments(self, issue: IssueRef) -> list[Comment]: ...
    @abstractmethod
    def post_comment(self, issue: IssueRef, body: str) -> str: ...       # returns URL
    def upsert_comment(self, issue: IssueRef, marker: str, body: str) -> str: ...  # optional (Renovate ensureComment)
    @abstractmethod
    def get_default_branch(self) -> str: ...
    @abstractmethod
    def add_labels(self, target: PRRef | IssueRef, labels: list[str]) -> None: ...
    def author_permission(self, login: str) -> Literal["owner", "member", "collaborator",
                                                      "contributor", "none", "unknown"]:
        return "unknown"   # optional; used by the worker when Author.association == "unknown" (§3.1)

class PullRequestClient(ABC):
    """OPTIONAL port: only exists when capabilities.supports_pr_api is True (§3.7)."""
    @abstractmethod
    def find_open_pr(self, branch: str) -> PRRef | None: ...             # absorbs the owner:ref quirk
    @abstractmethod
    def create_pr(self, issue: IssueRef, *, head: str, base: str, title: str,
                  body: str, draft: bool, labels: list[str]) -> PRRef: ...
    @abstractmethod
    def comment_on_pr(self, pr: PRRef, body: str) -> str: ...
    @abstractmethod
    def pr_has_conflicts(self, pr: PRRef) -> bool | None: ...

class GitTransport(ABC):
    @abstractmethod
    def clone_url(self, repo: str) -> str: ...
    @abstractmethod
    def git_auth_env(self, repo: str) -> dict[str, str]: ...   # correct host; token never in the URL
    @abstractmethod
    def pr_url(self, repo: str, number: int) -> str: ...
    @abstractmethod
    def compare_url(self, repo: str, base: str, head: str) -> str: ...
        # "open PR manually" link / branch diff; used in the flow with no PR API (§3.7)
    @abstractmethod
    def format_ref(self, kind: Literal["issue", "pr"], number: int, from_repo: str | None = None) -> str: ...
        # "#N" / "!N" / "owner/repo#N" (lesson from Atlantis MarkdownPullLink)
    def closing_reference(self, issue: IssueRef, target_repo: str) -> str | None: ...  # "Closes #N" or None

class ProviderPlugin(ABC):
    type: ClassVar[str]                       # "github", "gitlab", ...
    capabilities: ClassVar[ProviderCapabilities]

    @classmethod
    @abstractmethod
    def from_settings(cls, instance_id: str, settings: ProviderSettingsBase) -> "ProviderPlugin": ...
    webhook: WebhookAdapter
    @abstractmethod
    def client(self, repo: str) -> ForgeClient: ...        # resolves credentials PER REPO
    def pulls(self, repo: str) -> PullRequestClient | None:
        return None        # overridden by those with supports_pr_api=True; the registry validates coherence
    @abstractmethod
    def transport(self) -> GitTransport: ...
    @abstractmethod
    def bot_identity(self) -> Author: ...                  # login of the bot itself (is_self)
    @abstractmethod
    def health_check(self) -> ProviderHealth: ...
```

Design notes:

- **Per-repo credentials, inside the plugin.** `plugin.client(repo)` resolves (and
  renews) the token for the specific repo — eliminating the 5 `installation_id`
  fallbacks (§2.2) and the issues-vs-target inconsistency. Resolution
  happens **in the worker**, never in the webhook handler (removes the blocking network call
  from the ingestion hot path — Risk 2). Credentials expire (GitHub
  installation token ≈ 1h; GitLab OAuth ≈ 2h): the `ForgeClient` renews on demand.
- **No if-by-name in the consumer.** The `ToolchainManager` special-cases the
  `nodejs` plugin's constructor by name (`manager.py:40-48`) because the registry
  maps name→class without `from_settings`; here the registry uses
  `from_settings(instance_id, settings)` with per-plugin **typed** config.
- **Pattern:** ABC for the ports (the registry requires every method to exist);
  `typing.Protocol` only in tests for structural fakes.
- Triage/diagnosis/blocked comments (`triage/builder.py`,
  `diagnose/builder.py`, `worker.py::_post_blocked_comment`) use
  `ForgeClient.post_comment`; building the Markdown stays in the core, with
  references via `format_ref` and vocabulary via `capabilities.terms`; bodies
  above `max_body_length` are truncated in the core.
- `branch_name`, `build_pr_title`, `build_pr_body` and the commit message
  (`github/pr.py:45-116`, `executor.py:59-77`) **are not GitHub-specific** and
  **stay in the core**; only the `closing_reference` comes from the plugin. Draft: the core asks for
  `draft=True` and the plugin applies it according to `draft_mode` (native flag or title
  prefix).
- Auto-close **is not guaranteed** (configurable keywords on Gitea/self-managed
  GitLab; GitLab only closes on reaching the default branch): don't promise it in
  the PR text when `auto_close_keyword is None`.
- `supports_pr_api=False`: the harness pushes the branch and posts a comment on the
  issue with the `compare_url` instead of calling `create_pr`. The full flow is
  in §3.7. The PR operations live in a separate port (`PullRequestClient`),
  and not as `ForgeClient` methods that would raise `NotImplementedError`.
  This way the core decides by type (`plugin.pulls(repo) is None`), and the registry
  rejects at load time a plugin whose `capabilities.supports_pr_api` does not match the
  port.

**Implementation addendum (Phase 2a, 2026-10-25).** Differences from the
sketch above, all in `providers/` and `webhook/routes.py`:

- **Only the inbound ports exist.** `providers/base.py` brings
  `ProviderCapabilities`/`ProviderTerms`, `WebhookAdapter` and a
  `ProviderPlugin` with `type`, `capabilities`, `instance_id`, `webhook`,
  `from_settings` and `bot_identity`. `ForgeClient`, `GitTransport`,
  `PullRequestClient`, `client()`, `transport()`, `pulls()` and
  `health_check()` come in Phase 2b, together with the first implementation.
- **`WebhookAdapter`** has four methods: `delivery_key`,
  `verify_signature`, `event_name` and `parse_event`. The three that validate
  raise `WebhookRejectedError(error, log_event, mark_reason, status)`.
  `mark_reason=None` (missing key, invalid signature) never records the
  delivery. `parse_event` returns
  `ParsedDelivery(event, action, result: TriggerEvent | Ignored)`:
  `event`/`action` only feed logs and `processed_deliveries`.
  `Ignored(reason, repo=None)` indicates an event with no repo to route (unsupported
  type).
- **The adapter only normalizes.** The ingestion discards (comment on a PR,
  from the bot itself, on a closed issue, author not allowed with a known
  association, `label_added` without a trigger label) belong to the core, in
  `policy/ingestion.py`, on canonical fields. For that, `IssueRef` gained
  `state: "open" | "closed" | None`. No plugin receives the policy config.
- **Dispatcher order:** instance (404 if unknown) → `delivery_key`
  → `verify_signature` → duplicate → `parse_event` → repo route →
  `Ignored` → `ingestion_filter` → lifecycle → task. A redelivery
  of an unsupported event responds `duplicate`.
- **No network in the handler** (Risk 2). Without a credential hint, the task is
  written with `installation_id=0` and the worker resolves it, as it already did for
  migrated rows. The task's `credentials_ref` is the event's.
- **`from_settings(instance_id, settings)` receives the root `Settings`** until
  Phase 3 brings `providers.<id>`. `ProviderRegistry.from_settings` creates the
  implicit `github` instance; test plugins come in through the constructor.
- **Limitation until Phase 3:** `processed_deliveries` still has the PK
  `delivery_id`, without `provider_instance`. Two instances with the same key
  would collide, but today only `github` exists.

**Implementation addendum (Phase 2b, part 1, 2026-10-25).** The outbound
ports exist in `providers/base.py`, with these differences from the
sketch:

- **Clients bound to a repo.** `plugin.client(repo)` and
  `plugin.pulls(repo)` return clients for a single repo, so the methods
  receive the issue/PR **number** instead of an `IssueRef`
  (`get_issue_state(number)`, `list_comments(number, limit)`,
  `post_comment(number, body)`, `add_labels(number, labels)`). The worker and the
  reconciler only have `(repo, number)`, and that is enough.
- **Per-repo credential, inside the plugin.** GitHub resolves the installation
  for each repo (`GitHubAppAuth.installation_for`, with a per-process cache) and
  `GitTransport.git_auth_env(repo)` fetches a fresh token on each call. The
  core sees neither token nor `installation_id`. The plugin's `GitHubAppAuth` is
  created on first outbound use, never at web process startup.
- **`pr_has_conflicts(pr, head_sha)`** receives the freshly pushed SHA: right
  after the push GitHub may still answer for the previous head.
- **`create_pr` only creates and labels**; the core calls `find_open_pr` first. The
  labels go in only at creation, as before.
- **`format_ref(kind, number, *, repo, from_repo)`** qualifies the reference
  when it points to a repo other than the text's (`owner/repo#N`).
  `closing_reference(issue_repo, number, target_repo)` returns `None` when
  the forge does not reliably close the issue.
- **`ProviderHealth(ok, checks)`**: `health_check()` only looks at the config, with no
  network. Informational warnings start with `warning:` and do not bring down `ok`.
- **Coherence in the registry:** a plugin that overrides `pulls()` without
  `supports_pr_api=True` (or the other way around) is rejected at load time. The check is
  on the class, without needing a repo.
- **PR text in the core:** `core/pr_text.py` (`branch_name`,
  `commit_message`, `pr_title`, `pr_body`) receives the references already
  formatted by the plugin. GitHub's `PRService` keeps existing, only
  delegating to this module, until the executor migrates (2b-3).
- **Layout of `providers/github/`** differs from §3.4 in two points: the
  PR operations live in `pr.py`, separate from `client.py`; and
  `settings.py` only arrives with the per-instance config of Phase 3.

**Implementation addendum (Phase 2b, part 2, 2026-10-26).** The consumers
outside the executor now use the plugin:

- **Registry in the worker.** `worker.main()` creates the `ProviderRegistry` and passes it
  to `run_task`, `run_retry_recovery` and `reconcile_issue_work`. `run_task`
  picks the plugin by `task.provider_instance`; an unknown instance
  fails the task (`unhandled_worker_exception`).
- **Worker comments** (triage, blocked, retries exhausted) and
  diagnose comments go out through `plugin.client(task.repo).post_comment`, with the
  issues repo's credential. `task.installation_id` is no longer read outside the
  executor.
- **`Workspace(task_id, target_repo, settings, transport)`**: clone URL and
  credential come from the `GitTransport`. The slug becomes nested directories without
  `split("/")`, and a slug with `..` or an absolute one is rejected.
- **Reconciler:** `issue_work` does not yet record the instance (Phase 3), so
  every row is queried on the default instance (`github`).
- **`unknown` guard:** `check_comment_author_allowed` receives an
  `author_permission` callable; a failed lookup counts as `unknown` (fail
  closed).
- **`/readyz`** aggregates each instance's `health_check()`: each one's `config`
  goes into `settings`, the other checks become
  `<instance_id>_<check>`. `github_auth` keeps its name; the bot login warning
  becomes `github_own_bot_login`.

**Implementation addendum (Phase 2b, part 3, 2026-10-26).** The executor now
uses the plugin, with these choices:

- **`run_task(store, task, settings, plugin)`** builds one `_Outbound` per
  task: `client(task.repo)` to comment on the issue, `pulls(target_repo)` and
  `transport()` for the PR and git. Every body sent (comment, PR
  body, comment on the PR) goes through it and is cut at
  `capabilities.max_body_length` (`core/pr_text.fit_body`).
- **PR:** `find_open_pr(branch)` and, if there is none, `create_pr`. With
  `draft_mode="title_prefix"`, draft becomes `draft_prefix` in the title and
  `draft=False` in the API; with `none`, draft is ignored.
- **References:** commit and title use `format_ref("issue", ...)` as seen from the
  target repo; the body uses `closing_reference` (with no promise of closing
  when the forge returns `None`). The PR link in the issue comment is the
  `PRRef.url`.
- **git:** every authenticated call (`ls-remote`, `fetch`, push of the WIP and of the
  delivery) asks for `transport.git_auth_env(target_repo)` at that moment.
- **Comment history** is fetched once in `run_task` (and in
  diagnose) with `ForgeClient.list_comments`, still best-effort, and passed to
  `AgentRunner.run(comment_history=...)`. The runner does not access the network.
- **Forge with no PR API:** in this part, `run_task` failed the task
  (`FAILED_TERMINAL`) before cloning or spending LLM. Part 4 replaced
  that with the flow from §3.7.
- **Removed:** `get_shared_auth` (the `GitHubAppAuth` now lives in the plugin,
  one per process) and `PRService`, replaced by `GitHubPullRequestClient`.

**Implementation addendum (Phase 2b, part 4, 2026-10-26).** The flow from §3.7
was implemented in the executor, with these choices:

- **Choosing the path:** `run_task` decides once, by `out.pulls is None`.
  `_Outbound.delivery_reason` gives `pr_opened` or `branch_published`, and it is the
  same value used when closing the delivery and when returning a follow-up with the
  base merge unresolved.
- **Delivery without a PR:** the push is the same. Instead of the PR, a single comment on the
  issue carries the compare link, the summary, the files and the test results,
  because there is no PR body to keep them in. There is no promise of
  closing.
  - The PR labels (`pr.label` + the route's `labels`) go to the issue on the
    first delivery, if `supports_labels`. A follow-up does not reapply them.
  - The route's draft is ignored.
- **Follow-up:** `_open_or_resume_issue_work` reopens `pr_opened` or
  `branch_published` (`_DELIVERED_REASONS`), with the
  `issue_work_reopened_from_branch` event in the second case.
  - The initial comment and the `branch_status` say "published branch" instead
    of "Pull Request #N".
  - The delivery comment reports the base merge.
- **Conflict:** `_local_conflicts` fetches the base again (best-effort, with
  `git_auth_env`) and runs `git merge-tree --write-tree origin/<base> HEAD`.
  - Exit 1 only counts as a conflict when the output starts with the
    tree OID, because git also returns 1 for a nonexistent ref.
  - With a PR API, it is the fallback when `pr_has_conflicts` returns `None` or
    raises an exception.
  - The conflict note no longer mentions GitHub, and the log became
    `branch_still_conflicting`.
- **Vocabulary:** only the new text uses `terms.pr` ("No Merge Request was
  opened"). The prompts started using `terms` in Phase 4.

### 3.3 Static type registry + instances

`src/issue_agent/providers/registry.py`:

```python
BUILTIN_PROVIDER_TYPES: dict[str, type[ProviderPlugin]] = {
    "github": GitHubProviderPlugin,
}

class ProviderRegistry:
    """Configured instances (id -> plugin). Built in the FastAPI lifespan,
    stored in app.state and injected into the worker — replaced get_shared_auth()."""
    def __init__(self, settings: ProvidersSettings) -> None: ...
    def get(self, instance_id: str) -> ProviderPlugin: ...
    def all(self) -> Iterable[ProviderPlugin]: ...
```

- **Types** are static (a new type = one entry in the dictionary); **instances**
  come from config (`providers.<id>`). Plugin modules are imported **on demand**
  and the provider SDK is an optional pip extra (a lesson from PR-Agent) — this
  avoids pulling in `python-gitlab` (LGPL) for anyone who only uses GitHub.
- The registry is an injected instance — no module globals; the
  `_clear_shared_auth` fixture goes away.

### 3.4 GitHub plugin

All GitHub-specific code moves to `src/issue_agent/providers/github/`:

```
providers/github/
├── plugin.py          → GitHubProviderPlugin
├── webhook.py         ← webhook/parser.py + webhook/validation.py (WebhookAdapter)
├── auth.py            ← github/app_auth.py + github/auth_provider.py (per-installation token, cache)
├── client.py          ← github/client.py + operations from github/pr.py (ForgeClient)
├── transport.py       ← runner/git_auth.py + clone URL + pr_url + format_ref (GitTransport)
└── settings.py        → GitHubProviderSettings
```

What **stays in the core** (generic): `queue/`, `worker.py`,
`policy/label_router.py`, `policy/comment_filter.py` (over `Author`), `runner/`
(executor, agent_factory, environment, toolchains, summary, workspace),
building the PR branch/title/body, `observability/`, `config.py`
(the mechanism), `logging.py`.

`webhook/routes.py` becomes a generic dispatcher at
**`POST /webhooks/{instance_id}`**: the default `github` instance preserves the
GitHub App's current URL. **The provider is chosen by the path, never by
header sniffing** — Gitea and Forgejo also send `X-GitHub-Event`/`X-GitHub-Delivery`,
so header sniffing would confuse forges.

### 3.5 Configuration (breaking change, hard break)

**Before (actual shape of `config.yaml.example`):**

```yaml
github:
  app_id: 123
  app_bot_login: issue-agent[bot]
  host: https://github.com
github_app:
  token_refresh_margin_seconds: 300
agent:
  name: Issue Agent          # becomes github_app_name / the agent's display name
repos:
  - issues: owner/repo
    target: owner/repo
```

**After:**

```yaml
agent:
  name: Issue Agent          # stays: it is the agent's name, NOT the provider's
providers:
  github:                    # instance id (default; keeps /webhooks/github)
    type: github             # union discriminator
    app_id: 123
    bot_login: issue-agent[bot]
    host: https://github.com           # with scheme; GHE: https://ghe.example.com
    token_refresh_margin_seconds: 300
repos:
  - issues: owner/repo
    target: owner/repo
    provider: github         # instance id; default = the only configured instance,
                             # required if there is more than one
```

- `providers` is `dict[str, Annotated[Union[GitHubProviderSettings, ...], Field(discriminator="type")]]`
  (a pydantic discriminated union): typed config per plugin, no loose kwargs.
- Secrets remain **env-only**, never in the YAML (rule unchanged), namespaced
  per instance: `PROVIDERS__<ID>__APP_PRIVATE_KEY`,
  `PROVIDERS__<ID>__APP_PRIVATE_KEY_PATH`, `PROVIDERS__<ID>__WEBHOOK_SECRET`.
  This requires `env_nested_delimiter="__"` in `Settings.model_config` (absent
  today) and pydantic-settings' deep env+YAML merge — **validate with a spike
  test at the start of Phase 3**; no current alias contains `__`, so there is no
  collision. `app_private_key(_path)` is **not** read from the YAML.
- `agent.name` stays with the agent (today it feeds `github_app_name`, used in
  ~20 places: logging, comment headers, health).
- `RepoRoute` (`config.py:131-156`) gains `provider`; `route_for` becomes
  keyed by `(provider_instance, issues)`.
- `flatten_yaml_config` gains support for `providers.*`; the `github:` and
  `github_app:` sections and the root-level `github_*` fields are **removed**,
  including `github_installation_id` (dead config).
- Default git identity email: provided by the plugin, no longer hardcoded.
- An estimated 62 `GITHUB_APP_ID=` kwargs across 23 test files need to be
  migrated (central fixture helper).

**Implementation addendum (Phase 3, 2026-10-27).**

- **`from_settings(instance_id, config, settings)`**: the plugin receives its
  typed config (`providers.<id>`) and the root `Settings`, which only supplies
  what belongs to the agent (`agent_name` for the bot login fallback,
  `pr_label_agent_generated`). `GitHubAppAuth` receives only the instance's
  config.
- **`ProviderSettingsBase`** (`extra="forbid"`) declares `secret_fields`.
  `flatten_yaml_config` rejects those fields in the YAML, naming the env var to
  use in the message, and rejects the `github:`/`github_app:` sections, pointing
  to the guide. This way the hard break fails loudly instead of ignoring the
  config. The old env vars (`GITHUB_*`) are simply ignored: `/readyz` shows
  what is missing by the new name.
- **Instance id** restricted to `[a-z0-9]` with single `-`/`_` between
  parts: it goes into the webhook path and the env prefix, and `__` is the
  delimiter.
- **Routes:** `RepoRoute.provider` is filled with the single instance or is
  required; the same `issues` can be routed on two instances, but not twice on
  the same one. `Settings` with no instance and no route is valid (tests,
  booting without config).
- **`agent.name`** becomes `agent_name` / env `AGENT_NAME` (the maintainer's
  decision, taking advantage of the hard break).
- **Commit email:** `git_identity.user_email` defaults to empty, and the
  workspace uses `GitTransport.default_commit_email(user_name)`. GitHub
  returns the same value as before.
- **Backfill:** legacy rows are read as payloads of the `github` instance.
  Without that instance, the backfill does nothing and the worker guard fails
  the tasks. `credentials_ref` comes from the payload, as in ingestion.
- **`FakeProvider`** has no config type. Tests register it directly in the
  `ProviderRegistry` and rewire the routes after validation.

### 3.6 SQLite schema

The store uses `_migrate_v1.._migrate_v5` (table rebuild in v1–v3; additive in
v4–v5; no `user_version`), and `store.py:98-100` cites a "v2.0 §4.3
(P4)" restriction to additive-only migrations (the origin of this rule is not in
`docs/` — **document it or revoke it** in Phase 3). The strategy:

- **`tasks` — additive migration (v6):** `ALTER TABLE ADD COLUMN`
  `provider_instance TEXT NOT NULL DEFAULT 'github'`, `credentials_ref TEXT`,
  `trigger_json TEXT`. Backfill by re-normalizing the legacy `payload_json`
  through the GitHub plugin's parser (the raw payload is stored, so **every
  in-flight task can be re-normalized** — nothing is "unrecoverable" anymore).
  `installation_id` and `payload_json` are no longer read (legacy columns,
  removed in a future cleanup). *Done in v1.1.10:* the backfill was removed
  and migration `_migrate_v8` rebuilds `tasks` without both columns (another
  P4 exception, like v7).
- The **unique pending index** now includes `provider_instance`:
  `(provider_instance, repo, issue_number, trigger_kind) WHERE status='pending'`.
- **`processed_deliveries` and `issue_work` — rebuild** (v2-style): the PK becomes
  `(provider_instance, delivery_key)` and `(provider_instance, issue_repo, issue_number)`.
  This is an **explicit exception to the P4 restriction** (a PK cannot be altered
  additively); `processed_deliveries` is ephemeral, and `issue_work` is migrated
  with `provider_instance='github'`. Back up the volume before deploying (runbook).
- In-place migration when the store is opened, idempotent (existing pattern).

*Implemented in Phase 3 as `_migrate_v7`.* The pending index keeps its
name, so v7 recreates it when it does not cover `provider_instance`. The store
API takes the instance as its first argument (`is_delivery_processed`,
`mark_delivery_processed`, `*_issue_work`,
`has_active_creation_task_for_issue`). The P4 rule is now documented in
`docs/operations.md` § Schema migrations: additive migrations, except a PK
change, which rebuilds the table and makes the release one-way (rollback via
snapshot).

### 3.7 Forge with no PR API — a first-class flow in the core

Generic prerequisite added in the 2026-10-24 revision. The previous version
treated `supports_pr_api=False` as a capability. In the current code, however,
the entire lifecycle assumes a PR:

- `issue_work` closes with `closed_reason="pr_opened"` and stores `pr_number`;
- `_open_or_resume_issue_work` (`executor.py:1536`) only reopens the work on a
  follow-up when `closed_reason == "pr_opened"` (`executor.py:1573`);
- the follow-up reuses the branch and checks for conflicts via `pr_has_conflicts`
  (`executor.py:2521`);
- the result goes to the PR (`comment_on_pr`), and `build_branch_status` tells the
  agent that the branch "backs the open Pull Request #N" (`prompts.py:76`).

Without designing this into the core, the first forge with no PR API would violate
the "zero changes in `runner/`" criterion. The design belongs to **Phase 2b**:

| Aspect | With PR API | Without PR API |
|---|---|---|
| Delivery | `pulls.create_pr(...)` → `issue_work.closed_reason="pr_opened"`, `pr_number` | push the branch → `post_comment(issue, ...)` with `transport.compare_url(target, base, head)` → `closed_reason="branch_published"`, `pr_number=None` |
| Follow-up via comment | reopens `pr_opened` | reopens `pr_opened` **or** `branch_published` (same rule, same `work_branch`) |
| Conflict with the base | `pulls.pr_has_conflicts(pr)` | local check in the workspace: `git merge-tree --write-tree origin/<base> HEAD` (the harness already merges the base in `_update_branch_with_base`) |
| Where the result is published | `pulls.comment_on_pr(pr, ...)` | `post_comment(issue, ...)` |
| `agent-generated` labels | on the PR | only on the issue, if `supports_labels` |
| Text for the agent/human | "Pull Request #N" | `terms` vocabulary: "branch `<work_branch>`" + compare link, without promising auto-close |

Rules:

- The core picks the path **once per task**, by `plugin.pulls(repo) is
  None`. There is no `if provider == ...`.
- The local conflict check is the source of truth when there is no PR API, and
  is also acceptable as a fallback when `pr_has_conflicts` returns `None`.
- `closed_reason="branch_published"` is additive (text column, no schema
  migration).
- The contract suite (§5) exercises both paths via `FakeProvider`, before a
  real provider with no PR API exists.

---

## 4. Phased migration plan

Each phase is one or more atomic PRs, always green on `ruff check` +
`uv run pytest`, following the `AGENTS.md` flow (branch from `origin/main`,
conventional commits, version bump per PR).

### Phase 0 — Fix latent bugs (`fix`, patch) — ✅ done

Independent of the refactor, and it reduces what the migration needs to preserve (§2.6):

- GHE: correct `host` in `git_auth_env`, `base_url` in `GithubIntegration`/`Github`,
  PR URL derived from `github_host` (tests with a custom host).
- Do not mark the delivery as processed before the signature is valid.
- Declare `litellm` in `pyproject.toml`.
- Remove dead config (`github_installation_id`).
- **Characterization tests** for the `executor.py` flow (follow-up/base-merge)
  before Phase 2b.

### Phase 1 — Canonical model and normalization at ingestion — ✅ done (in three PRs: 1a, 1b, 1c)

- Create `core/models.py` (`Author`, `Comment`, `IssueRef`, `TriggerEvent`),
  evolving `ParsedWebhook`.
- The GitHub webhook parser now produces `TriggerEvent`; the filters in
  `routes.py:209-263` (PR-comment, own-bot, closed, association) move into the
  parser (`parse_event → None`).
- **Additive** SQLite migration v6 (`trigger_json`, `provider_instance`,
  `credentials_ref`) with backfill; `tasks.payload_json` is no longer read.
- Migrate the consumers from §2.1 (including `webhook/parser.py`,
  `webhook/routes.py`, `runner/diagnose.py` and the `list_issue_comments` dicts
  → canonical `Comment`).
- `policy/comment_filter.py` now uses `Author.association`/`is_self`, with
  the `association="unknown"` path deferred to the worker (§3.1).
- `label_added` with the "the label set changed" semantics (§3.1): the core
  decides by the current set, and an event without a trigger label becomes a no-op.
- **No** module moves yet — GitHub stays in `github/` and
  `webhook/`.

### Phase 2a — Interface, registry and webhook ingress — ✅ done (v0.11.0)

- Create `providers/base.py` (ports from §3.2) and `providers/registry.py`.
- Move `webhook/parser.py` + `webhook/validation.py` to
  `providers/github/webhook.py` (`WebhookAdapter`); `routes.py` becomes the
  `POST /webhooks/{instance_id}` dispatcher (default `github` keeps the URL).
- Idempotency by the plugin's `delivery_key`.
- `FakeProvider` in `tests/fakes/` and the contract suite (§5).

### Phase 2b — Egress: `ForgeClient` and `GitTransport` — ✅ done (v0.12.0–v0.13.0)

- Move `github/client.py`, `github/pr.py` (API operations) and
  `runner/git_auth.py` to `providers/github/`.
- `worker.py`, `runner/executor.py`, `runner/diagnose.py`,
  `observability/cleanup.py` and `webhook/health.py` now talk to the plugin
  through the interface — including the follow-up flow (`find_open_pr`,
  `comment_on_pr`, `pr_has_conflicts`, `get_default_branch`).
- Eliminate `owner/repo` splits in the core, the hardcoded PR URL
  (`transport.pr_url`) and clone URL (`transport.clone_url`).
- Building the PR branch/title/body stays in the core.
- PR operations behind the optional `PullRequestClient` port and the no-PR-API
  flow (§3.7): `closed_reason="branch_published"`, follow-up reopening that
  state, local conflict check via `git merge-tree`, comment with
  `transport.compare_url`. Covered by the `FakeProvider` without PR API.

### Phase 3 — Per-instance config and credentials (breaking) — ✅ done (v1.0.0)

- Spike test for `env_nested_delimiter="__"` + env/YAML merge (§3.5).
- `providers.*` section (discriminated union) + new env vars; removal of the
  root-level `github_*` fields and the `github:`/`github_app:` sections (hard break).
- Migration v6b: rebuild `processed_deliveries` and `issue_work` with
  `provider_instance` in the PK (§3.6).
- `ProviderRegistry` built in the lifespan and injected; removal of
  `get_shared_auth`; credentials resolved per repo inside the plugin and in the
  worker (never in the webhook handler).
- Document (or revoke) the "P4" restriction.
- Migration guide (§8) published in `docs/operations.md`.

### Phase 4 — Cleanup and final generalizations — ✅ done (v1.1.0)

- Prompts parameterized by `capabilities.terms` (`issue`/`pr` vocabulary,
  reference prefix, repo instructions files) in `prompts/system.md`,
  `system_diagnose.md`, per-label templates and `INSTANCE_TEMPLATE`.
- Health/readiness iterating over the enabled instances.
- Metrics/audit with a `provider_instance` label alongside `repo`.
- Update `AGENTS.md` (directory structure, plugins section) and
  `docs/operations.md`.

---

## 5. Testing strategy

Following the test discipline in `AGENTS.md`:

- **FakeProvider:** `tests/fakes/fake_provider.py` — an in-memory implementation of
  the ports. Today there is no GitHub fake (121 references to `get_shared_auth`); the
  core tests (`test_worker`, `test_executor`, `test_label_router`,
  `test_guards`) switch to the canonical model and the `FakeProvider`. The fake
  has **two variants**: with `PullRequestClient` and without (`supports_pr_api=False`,
  `author_permission` → `"unknown"`, labels without a diff). This way the generic
  prerequisites from §3.1 and §3.7 are tested in the core before the first real
  provider that depends on them.
- **Per-plugin unit tests:** mirror the GitHub-coupled tests into
  `tests/unit/providers/github/` (`test_webhook`, `test_app_auth`,
  `test_client`, `test_pr_ops`, `test_transport`).
- **Conformance contract:** `tests/unit/providers/test_provider_contract.py`,
  **parameterized over `BUILTIN_PROVIDER_TYPES`**, with per-plugin fixtures
  (`tests/unit/providers/<type>/fixtures/*.json` + headers). It covers:
  - `type`/`capabilities` present and valid;
  - valid signature, invalid signature, and **empty secret ⇒ rejects** (fail-closed);
  - `delivery_key` stable across redeliveries of the same fixture;
  - `parse_event` returns a valid `TriggerEvent`; event on a PR ⇒ `target_kind="pr"`;
    ignored/own-bot event ⇒ `None`;
  - well-formed `pr_url`, `format_ref`, `clone_url`; `git_auth_env` with a custom
    host;
  - body above `max_body_length` truncated by the core;
  - `supports_pr_api` consistent with `pulls()` (the registry rejects a mismatch);
    well-formed `compare_url`;
  - "labels changed" event without a trigger label ⇒ no task runs;
    author with `association="unknown"` ⇒ filter applied in the worker;
  - `health_check` does not raise with minimal config.
  This suite is the mechanism that makes GitLab/Gitea/Gogs "plug-and-play".
- **Integration:** `make_github_payload()`/`sign_payload()` from
  `tests/integration/conftest.py` become GitHub plugin helpers; the end-to-end
  tests (`test_github_stack`, `test_full_flow_fake_llm`) must pass
  **without changing behavior assertions**. Realistic caveat: the
  assertions stay, but the *patch targets* (`get_shared_auth`,
  `GitHubClient`, `PRService`) change — "zero functional diff" applies to
  behavior, not to mocks.
- **E2E:** the existing ones (real GitHub) stay opt-in and must pass after the
  migration.
- **Test config:** keep the `_SettingsNoEnv` pattern (a subclass with null
  `env_file`/`yaml_file`); a central helper to build `providers.*`
  in tests (replaces the 62 `GITHUB_APP_ID` kwargs).
- **SQLite migration:** v6/v6b tests over fixture databases from earlier
  versions, including re-normalized `pending`/`running` tasks.
- **Characterization tests** for `executor.py` (Phase 0) as the safety net
  for Phase 2b.

---

## 6. Acceptance criteria

1. All code that imports PyGithub or knows the shape of the GitHub webhook lives
   in `src/issue_agent/providers/github/`.
2. Static check (`grep`) outside `providers/` with no occurrences of:
   `payload[`, `raw_payload`, `payload_json`, `import github`/`from github`,
   `x-access-token`, `github.com`, `installation_id`, `author_association`,
   `performed_via_github_app`, nor `.split("/")` over repo slugs.
3. The provider conformance suite runs and passes against the GitHub plugin (and
   against the `FakeProvider`).
4. Behavior preserved: all existing unit/integration tests pass
   with the same behavior assertions (only imports, fixtures
   and patch targets adapted); the GitHub opt-in e2e passes.
5. `ruff check` and `uv run pytest` green in every phase.
6. GHE works (Phase 0): clone/push/API/PR URL respect `host`.
7. Config migration guide published; `AGENTS.md` and `docs/operations.md`
   updated.
8. (Architectural) A new provider can be added without touching the core —
   demonstrated by design review in Phase 2 and validated by the contract
   suite, **including a forge with no PR API, no `author_association` in the
   webhook and no label diff** (`FakeProvider` variants, §5).

---

## 7. Risks and mitigations

| # | Risk | Mitigation |
|---|---|---|
| 1 | The `payload_json` refactor touches ≥ 9 places — highest regression risk | Phase 1 isolated, no file moves; preserved behavior as a gate; characterization tests |
| 2 | `installation_id` resolution in 5 places, one of them with network access on the webhook hot path, with an issues-vs-target inconsistency | `client(repo)` resolves credentials per repo inside the plugin, in the worker (Phase 2b/3); webhook synchronous and network-free |
| 3 | Global singletons (`get_shared_auth`, `get_settings`) assume 1 App; 118 patches in the tests | `ProviderRegistry` injected via lifespan; central fixture helper; batch test migration in Phase 3 |
| 4 | `git_auth.py` assumes `x-access-token:` and the github.com host (GitLab uses `oauth2:`; GHE broken today) | `GitTransport.git_auth_env(repo)` with the correct host; GHE fix in Phase 0 |
| 5 | Idempotency by `X-GitHub-Delivery`; Gitea may change the id on redelivery | `delivery_key` in the plugin; PK `(provider_instance, delivery_key)`; optional content-based key |
| 6 | "GitHub issue"/"Pull Request"/`.github/copilot-instructions.md` vocabulary in the prompts | `capabilities.terms` injected into the templates in Phase 4 |
| 7 | Health check and reconciliation call the GitHub API outside the runner | `health_check`, `get_issue_state` in the ports |
| 8 | In-flight tasks during the schema migration | The legacy raw payload can be re-normalized by the plugin's parser (v6 backfill); volume backup and deploy with an empty queue, documented in the runbook |
| 9 | "P4" restriction (additive migrations) vs. PK change | Explicit, justified exception for `processed_deliveries`/`issue_work`; restriction documented/revoked in Phase 3 |
| 10 | Webhook URL configured in the GitHub App (`/webhooks/github`) | Default `github` instance ⇒ path unchanged |
| 11 | **Provider spoofing via headers** (Gitea/Forgejo send `X-GitHub-*`) | Provider chosen by the **path** `/webhooks/{instance_id}`, never by header |
| 12 | **Empty secret** (Forgejo sends an empty signature with no secret) | `verify_signature` fails closed; covered by the contract suite |
| 13 | **Concentration in `executor.py`** (2.5k LOC) | Dedicated Phase 2b; prior characterization; small PRs per operation |
| 14 | **Semver** — `AGENTS.md` defines `major = breaking`, Phase 3 is breaking | Explicit decision at the Phase 3 release (§8) |
| 15 | Scope creep: designing GitLab/Gogs in detail now | Out of scope; the design only guarantees the ports and the contract suite; the roadmap (§9) lists the requirements already mapped; Gogs has its own PRD ([`PRD-gogs.md`](./PRD-gogs.md)) |
| 16 | Ports shaped only by GitHub: a forge with no PR API, no author association or no label diff would require changing the core later | Generic prerequisites (§3.1, §3.7) land in Phases 1 and 2b and are exercised by the `FakeProvider` without PR API (§5) |

---

## 8. Configuration migration guide (summary)

| Before (removed) | After |
|---|---|
| `github.app_id` (YAML) / `GITHUB_APP_ID` | `providers.<id>.app_id` / `PROVIDERS__<ID>__APP_ID` |
| `GITHUB_APP_PRIVATE_KEY` / `GITHUB_APP_PRIVATE_KEY_PATH` (**env only**) | `PROVIDERS__<ID>__APP_PRIVATE_KEY` / `..._PATH` (env only) |
| `GITHUB_WEBHOOK_SECRET` (env only) | `PROVIDERS__<ID>__WEBHOOK_SECRET` (env only) |
| `github.app_bot_login` / `GITHUB_APP_BOT_LOGIN` | `providers.<id>.bot_login` |
| `github.host` / `GITHUB_HOST` | `providers.<id>.host` (with scheme) |
| `github_app.token_refresh_margin_seconds` | `providers.<id>.token_refresh_margin_seconds` |
| `GITHUB_INSTALLATION_ID` (dead config) | removed |
| `agent.name` / `GITHUB_APP_NAME` | `agent.name` (unchanged — it does not belong to the provider) / `AGENT_NAME` |
| `repos[].issues` / `repos[].target` | unchanged; `repos[].provider` = instance id (default: the single instance) |

- `<ID>` is the instance id in uppercase (`github` ⇒ `GITHUB`).
- `fly secrets set` must be redone with the new names and the old ones removed
  (`fly secrets unset`) in the Phase 3 deploy (a step in the runbook). Since it is
  a hard break, there is no alias period.
- **Semver (decided):** `AGENTS.md` defines `major = breaking`; Phase 3 is
  **1.0.0**, called out explicitly in the release notes. The other phases
  follow patch (fix) / minor (feat).
- The full guide, with the deploy steps, is in `docs/operations.md`
  § Upgrading to 1.0.0.

---

## 9. Future roadmap (out of scope for this PRD)

Architecture validators, in suggested order. The requirements below have already
been mapped in the research and guide the design of the ports, but they are **not**
implemented here:

1. **GitHub PR trigger** — same provider, new events (`pull_request`,
   `pull_request_review_comment`) in the GitHub plugin's `parse_event`. Validates
   that the core handles `target_kind="pr"` and new `TriggerKind`s without
   structural change.
2. **GitLab** — the first truly new provider:
   - Webhook: legacy `X-Gitlab-Token` token (constant-time comparison) **and**
     GitLab 19.x signing token (Standard Webhooks: HMAC over
     `webhook-id.webhook-timestamp.body` + anti-replay window; the
     `standardwebhooks` library); idempotency `webhook-id` > `Idempotency-Key` >
     `X-Gitlab-Event-UUID`; optional: project-bound token (packit).
   - Numbers via `iid`; nested groups (`g/sub/proj`); `Note Hook` with
     `noteable_type` ⇒ `target_kind`; added labels computed from
     `changes.labels.{previous,current}`.
   - Auth: no GitHub App equivalent; use a service account/project access
     token; `git_auth_env` with `oauth2:<token>`; "Merge Request" vocabulary,
     `!N` prefix; draft via `title_prefix` ("Draft: "); conditional auto-close.
   - Per-project/group webhook (optional `ensure_webhook`).
3. **Gitea/Forgejo** — same header family (`X-Gitea-Signature` HMAC-SHA256
   without prefix, `X-Gitea-Delivery`/`X-Forgejo-Delivery`), PR API, draft via
   `WIP:` prefix, per-repo token. Provider chosen by path (`X-GitHub-*`
   headers are spoofed by these forges).
4. **Gogs**: specified in its own PRD, [`PRD-gogs.md`](./PRD-gogs.md).
   Depends on Phases 1–3, on the Phase 4 `terms`, and on the generic prerequisites
   from §3.1/§3.7. It is the first new provider by the maintainer's decision
   (real demand), ahead of GitLab and Gitea/Forgejo, despite its position in this list.

Each one must pass the contract suite
(`tests/unit/providers/test_provider_contract.py`) without any change to the
core — that is the definitive test of the architecture.

---

## Appendix A — Prior art and libraries evaluated

**Conclusion:** no library replaces the proposed interface. The decision is to
write our own `ProviderPlugin`, **borrow design** from mature projects, and
use SDKs only **inside** each plugin.

| Decision | Item | Use / notes |
|---|---|---|
| Borrow design | [packit-service](https://github.com/packit/packit-service) (`events/{abstract,github,gitlab,forgejo}`, MIT) | Abstract events with per-forge specialization; small events with the object fetched on demand; project-bound GitLab token |
| Borrow design | [PR-Agent](https://github.com/The-PR-Agent/pr-agent) (MIT) | Same shape (FastAPI + `GitProvider` ABC + per-forge webhook server); static registry with lazy import and pip extras; Gitea refuses without a secret (fail-closed) |
| Borrow design | [Renovate `Platform`](https://github.com/renovatebot/renovate/blob/main/lib/modules/platform/types.ts) (TS) | Per-forge `maxBodyLength`, `ensureComment` (upsert by marker), `getBranchPr` (reuse PR), draft via title prefix |
| Borrow design | [Atlantis VCS client](https://github.com/runatlantis/atlantis/blob/main/server/events/vcs/client.go) (Go) | Parsing separate from the API client; `MarkdownPullLink` (`#N` vs `!N`); `GetCloneURL` |
| Ideas only | [OpenHands integrations](https://github.com/OpenHands/sandbox-server/tree/main/openhands/app_server/integrations) (NOASSERTION license — do not copy code) | `get_provider_terms()` for prompts; per-provider git credential usernames |
| Optional, inside the plugin | [ogr](https://github.com/packit/ogr) 0.63 (MIT, Py≥3.12; GitHub/GitLab/Forgejo) | Covers only the API-call half; **no** webhooks, **no** `draft` in `create_pr`, no Gogs; heavy deps (python-gitlab is LGPL). Evaluate in the GitLab/Forgejo plugin; never as the interface |
| Adopt with GitLab | [standardwebhooks](https://pypi.org/project/standardwebhooks/) 1.1 (MIT) | Verification of the GitLab 19.x signing token |
| SDK per plugin | PyGithub (keep), python-gitlab 8.6, pyforgejo 2.0 | No churn in the GitHub plugin |
| Optional | [githubkit](https://github.com/yanyongyu/githubkit) | Typed `webhooks.verify/parse`; does not justify replacing PyGithub now |
| Reject | giteapy (stalled since 2020), octomachinery (GPLv3, stalled), gidgethub (maintenance mode), pluggy/stevedore/entry points | Built-in plugins do not need dynamic discovery |

**Named patterns:** Ports & Adapters + Anti-Corruption Layer (canonical
model); Abstract Factory/Registry with typed `from_settings`; Strategy per
instance; pydantic discriminated union for per-instance config;
`typing.Protocol` only for structural fakes; DI via the FastAPI lifespan instead
of a global singleton.

**Verified webhook facts (research 2026-10-19; the Gogs row rechecked against the `gogs/gogs` source on 2026-10-24):**

| Forge | Authentication | Delivery id |
|---|---|---|
| GitHub | `X-Hub-Signature-256: sha256=<hex>` | `X-GitHub-Delivery` (stable on manual redelivery) |
| GitLab | `X-Gitlab-Token` (plain secret) or, 19.x, `webhook-signature: v1,<b64>` (Standard Webhooks) | `webhook-id` / `Idempotency-Key` (17.4+) stable across retries; `X-Gitlab-Event-UUID` is shared by recursive hooks |
| Gitea | `X-Gitea-Signature` (hex HMAC-SHA256); also sends `X-GitHub-*` and `X-Gogs-*` | `X-Gitea-Delivery` (not confirmed whether stable on redelivery) |
| Forgejo | `X-Forgejo-*`, `X-Gitea-*`, `X-Gogs-*`, `X-Hub-Signature(-256)`, `X-GitHub-*`; **empty signature with no secret** | `X-Forgejo-Delivery` |
| Gogs | `X-Gogs-Signature` (hex HMAC-SHA256 of the body, **empty with no secret**); also sends `X-Github-Event`/`X-Github-Delivery` | `X-Gogs-Delivery` (UUID per hook task; stability on redelivery not verified) |

*Not verified:* compatibility of `ogr`'s Forgejo backend with Gitea;
stability of Gitea's delivery id on redelivery; exact content of Gitea's
`label_updated` payload; native draft parameter in GitLab's MR API.
