# PRD — Gogs Provider

**Status:** Draft for future implementation. **Blocked** by Phases 1–3
of the [provider plugins PRD](./PRD.md), by the Phase 4 `terms`, and by the
generic prerequisites in §3.1/§3.7 of that PRD.
**Date:** 2026-10-24
**Type:** `feat` (new provider; no change to the core)

## Revision history

| Date | Change |
|---|---|
| 2026-10-24 | Original version. Gogs facts verified against the source (`gogs/gogs` `internal/route/api/v1/api.go`, `internal/database/webhook.go`; `gogs/go-gogs-client` `issue.go`, `repo_hook.go`). Owner's decision: Gogs is the first new provider, because there is real demand for it. |

---

## 1. Vision and goals

Let fly-swe serve repositories hosted on a self-hosted **Gogs** server, with
the same end-to-end flow as GitHub: a labeled issue (or a command in a
comment) becomes a task, the agent fixes it, and the result comes back to the
issue.

### Goals

1. Implement `src/issue_agent/providers/gogs/` as a `ProviderPlugin`
   (`PRD.md` §3.2) registered in `BUILTIN_PROVIDER_TYPES`.
2. Use the core's **no-PR-API** flow (`PRD.md` §3.7): Gogs has no Pull
   Request endpoint, so delivery is a branch push + a comment on the issue
   with a compare link for a human to open the PR through the UI.
3. Pass the contract suite (`tests/unit/providers/test_provider_contract.py`)
   and an opt-in e2e against a real Gogs in a container.

### Success criterion

> The Gogs plugin is added **without any change** outside
> `src/issue_agent/providers/gogs/`, `tests/**/providers/gogs/` and the type
> registration (entry in `BUILTIN_PROVIDER_TYPES` + member of the
> `providers.*` discriminated union), plus documentation and config examples.
> If the implementation requires changing `queue/`, `worker.py`, `runner/`,
> `policy/` or `observability/`, that is a defect in the providers PRD: it is
> fixed there, in a separate PR, before continuing here.

### Non-goals

- Creating Pull Requests on Gogs by any unofficial route (web routes with
  session/CSRF, scraping). No API, no automatic PR.
- Triggering on Gogs PR events (`pull_request`): out of scope, as in the
  GitHub provider.
- Supporting Gitea/Forgejo through this plugin. They have a PR API and their
  own headers and will be a separate type (`PRD.md` §9), even though they
  share part of the payload shape.
- Managing webhooks through the API (`ensure_webhook`): the operator
  configures the webhook on the repository.

---

## 2. Prerequisites (in the core, via `PRD.md`)

| Prerequisite | Where | Why Gogs needs it |
|---|---|---|
| Canonical model and `parse_event` | Phase 1 | The Gogs payload must not leak into the core |
| `label_added` = "the label set changed" | §3.1, Phase 1 | Gogs only emits `label_updated` with the current labels, no diff |
| `Author.association="unknown"` + filter deferred to the worker | §3.1, Phase 1 | The Gogs webhook has no `author_association` |
| Ports, registry, `POST /webhooks/{instance_id}` | Phase 2a | Per-instance route; Gogs also sends `X-Github-*` |
| Optional `PullRequestClient` + no-PR-API flow | §3.7, Phase 2b | The Gogs v1 API has no PR routes |
| Per-instance `providers.*`, secrets in `PROVIDERS__<ID>__*` | Phase 3 | Per-instance config and credentials |
| `capabilities.terms` in the prompts | Phase 4 (partial) | The prompts must not talk about an opened "Pull Request" |

Until these items are on `main`, this PRD does not start.

---

## 3. Gogs facts (verified against the source on 2026-10-24)

### 3.1 v1 API (`internal/route/api/v1/api.go`)

| Resource | Routes | Note |
|---|---|---|
| Pull Requests | **none** | Any `/pulls` falls through to the catch-all and returns 404 |
| Issues | `GET, POST /repos/:o/:r/issues`; `GET, PATCH /issues/:index` | Close/reopen via `PATCH` `state` |
| Comments | `GET, POST /issues/:index/comments`; `PATCH, DELETE /issues/:index/comments/:id` | `PATCH` enables `upsert_comment` |
| Issue labels | `GET /issues/:index/labels`; `POST, PUT, DELETE /issues/:index/labels`; `DELETE .../labels/:id` | Writing requires *writer*; the body uses label **IDs**, not names |
| Repo labels | `GET /labels`, `GET /labels/:id` (+ write for *writer*) | Basis for the name→ID mapping |
| Collaborators | `GET /collaborators`, `GET, PUT, DELETE /collaborators/:user` | **Requires admin** on the repo |
| Branches | `GET /branches`, `GET /branches/*` | — |
| Repo | `GET /repos/:o/:r` | Returns `default_branch`, `clone_url`, `html_url` |

API authentication: `Authorization: token <access-token>`.

### 3.2 Webhooks (`internal/database/webhook.go`, `go-gogs-client/repo_hook.go`)

- **Headers:** `X-Gogs-Event`, `X-Gogs-Delivery` and `X-Gogs-Signature`, plus
  `X-Github-Event` and `X-Github-Delivery` with the same values. That is why
  the provider is chosen **by path** (`/webhooks/{instance_id}`), never by
  header.
- **Signature:** hex HMAC-SHA256, no prefix, over the body and with the
  webhook secret. It is computed when the task is enqueued. **It is sent
  empty when the webhook has no secret**, so verification must fail closed.
- **Delivery:** one UUID per hook task. It has not been verified whether the
  UI's "Redeliver" reuses the UUID (§9).
- **Events:** `create`, `delete`, `fork`, `push`, `issues`, `pull_request`,
  `issue_comment`, `release`.
- **`issues` actions:** `opened`, `closed`, `reopened`, `edited`, `assigned`,
  `unassigned`, `label_updated`, `label_cleared`, `milestoned`, `demilestoned`,
  `synchronized`.
- **`IssuesPayload`:** `action`, `number`, `issue`, `changes` (title and body
  only), `repository` and `sender`. **There is no label diff.**
- **`IssueCommentPayload`:** `action`, `issue`, `comment`, `changes`,
  `repository` and `sender`.
- **`Issue`:** `id`, `number`, `user`, `title`, `body`, `labels`, `state`,
  `created_at`, `updated_at`, `pull_request` (present when the "issue" is a
  PR).
- There is no `author_association` and no `performed_via_github_app`.

### 3.3 Ecosystem

- There is no maintained Python SDK for the Gogs API. The plugin uses `httpx`,
  which is already a direct dependency of the project.
- Gogs evolves at a much slower pace than Gitea/Forgejo. Keep the plugin
  small and covered by real fixtures, so that breakage from a new version
  shows up in the e2e.

---

## 4. Plugin architecture

```
providers/gogs/
├── plugin.py      → GogsProviderPlugin (type="gogs", capabilities, from_settings, client, transport, bot_identity, health_check)
├── settings.py    → GogsProviderSettings (member of the providers.* discriminated union)
├── webhook.py     → GogsWebhookAdapter (verify_signature, delivery_key, parse_event)
├── client.py      → GogsForgeClient (httpx; issues, comments, labels, default branch, author_permission)
└── transport.py   → GogsGitTransport (clone_url, git_auth_env, compare_url, format_ref, pr_url)
```

`pulls()` is not overridden, so it returns `None`: the core follows the
no-PR-API flow (`PRD.md` §3.7).

### 4.1 Capabilities

| Field | Value | Reason |
|---|---|---|
| `supports_pr_api` | `False` | No PR routes |
| `draft_mode` | `"none"` | There is no PR to create |
| `supports_labels` | `True` | Labels on the issue via the API (by ID) |
| `auto_close_keyword` | `None` | There is no bot PR; nothing to promise |
| `max_body_length` | `65_000` (conservative) | Actual limit not verified (§9) |
| `terms.issue` / `terms.pr` | `"issue"` / `"Pull Request"` | Gogs UI vocabulary |
| `terms.ref_prefix_issue` / `ref_prefix_pr` | `"#"` / `"#"` | Gogs numbers issues and PRs in the same index |
| `terms.repo_instructions_files` | `[]` (only the core's generic ones; see §9) | Gogs has no convention equivalent to `.github/copilot-instructions.md` |

### 4.2 `WebhookAdapter`

- `verify_signature`: requires a configured `webhook_secret` and a non-empty
  `X-Gogs-Signature`; compares
  `hmac.new(secret, body, sha256).hexdigest()` with `hmac.compare_digest`.
  Anything missing ⇒ reject.
- `delivery_key`: `X-Gogs-Delivery`. If redelivery generates a new UUID (§9),
  a content-based key (`event + action + repo + number + issue.updated_at`)
  prevents double execution. The decision is left to phase G1, after
  measuring.
- `parse_event`:

| Event / action | `TriggerEvent.kind` | Rules |
|---|---|---|
| `issues` / `opened` | `issue_opened` | — |
| `issues` / `label_updated` | `label_added` | Only if there is ≥ 1 label; `IssueRef.labels` = current set (`PRD.md` §3.1) |
| `issues` / `label_cleared` | `None` | No labels, nothing to classify |
| `issues` / `closed`, `reopened` | `issue_closed`, `issue_reopened` | `issue_work` lifecycle, as today |
| `issue_comment` / `created` | `comment_command` | `target_kind="pr"` if `issue.pull_request` is present ⇒ `None` (the core ignores comments on PRs); author = bot ⇒ `None`; closed issue ⇒ `None` |
| others | `None` | Ack 200 with a reason, like the GitHub provider |

  - `Author.association = "unknown"` always, and
    `Author.is_self = (login == bot_login)`, compared case-insensitively.
  - `repo = repository.full_name` (`owner/name`; Gogs has no nested groups,
    but the core still treats the slug as opaque).
  - `IssueRef.url` is built by the plugin (`{host}/{owner}/{repo}/issues/{n}`):
    the Gogs API `Issue` does not include `html_url`.

### 4.3 `ForgeClient`

| Method | Implementation |
|---|---|
| `get_issue_state` | `GET /repos/:o/:r/issues/:n` → `state` |
| `list_comments` | `GET .../issues/:n/comments` → `Comment` (association `"unknown"`, `is_self` by login) |
| `post_comment` | `POST .../issues/:n/comments` → comment URL |
| `upsert_comment` | list and look for the marker in the body → `PATCH .../comments/:id` or `POST` |
| `get_default_branch` | `GET /repos/:o/:r` → `default_branch` |
| `add_labels` | `GET /labels` (cached per repo) → names→IDs → `POST .../issues/:n/labels {"labels": [ids]}`; nonexistent name ⇒ log + ignore (does not create the label) |
| `author_permission` | `api` mode: `GET /collaborators/:login` (204 ⇒ `"collaborator"`, 404 ⇒ `"none"`), cached per `(repo, login)`; `allowlist` mode: `"unknown"` |

HTTP errors: 401/403 ⇒ credential error (no retry); 404 ⇒ missing resource;
5xx/timeouts ⇒ recoverable, under the worker's existing retry policy.

### 4.4 `GitTransport`

| Method | Value |
|---|---|
| `clone_url(repo)` | `{host}/{repo}.git` |
| `git_auth_env(repo)` | `http.<host>.extraHeader: Authorization: Basic base64("<bot_login>:<token>")` via `GIT_CONFIG_*` (same technique as `runner/git_auth.py`: the token is never in the URL or on disk) |
| `compare_url(repo, base, head)` | `{host}/{repo}/compare/{base}...{head}` |
| `pr_url(repo, n)` | `{host}/{repo}/pulls/{n}`; only used if a human provides the PR (there is no automatic flow) |
| `format_ref("issue", n, from_repo)` | `#n`, or `owner/repo#n` across repos |
| `closing_reference` | `None` |

### 4.5 Identity and credentials

- A **bot user account** on Gogs, with an access token and *write* access to
  the issues and target repos. With `author_permission: api` mode, it also
  needs *admin* (§3.1).
- The token does not expire: `client(repo)` does not need to refresh it. An
  auth error becomes a terminal failure with an alert.
- The git identity email comes from the plugin: default
  `<bot_login>@users.noreply.<hostname>`, configurable.

### 4.6 Configuration

```yaml
providers:
  gogs-corp:                     # instance id → POST /webhooks/gogs-corp
    type: gogs
    host: https://git.example.com          # with scheme; API at {host}/api/v1
    bot_login: issue-agent
    author_permission: allowlist           # allowlist | api (api requires bot admin)
    git_user_email: issue-agent@example.com   # optional
repos:
  - issues: team/app
    target: team/app
    provider: gogs-corp
```

Secrets only in env (project rule): `PROVIDERS__GOGS_CORP__TOKEN` and
`PROVIDERS__GOGS_CORP__WEBHOOK_SECRET`. How ids with a hyphen become env
names is still to be defined, in Phase 3 of `PRD.md` (§9).

### 4.7 Comment-author policy

`policy/comment_filter.py` already accepts a comment when any condition
holds: filter disabled, issue author, allowed association, or login on the
allowlist. With `association="unknown"`:

- **`allowlist` mode (default):** the issue author and
  `comments_author_filter_allowlist` apply. Does not require bot admin.
- **`api` mode:** the worker queries `author_permission`; `"collaborator"`
  counts as an allowed association if `COLLABORATOR` is in
  `comments_author_filter_allow_associations`.

The runbook (`docs/operations.md`) documents both modes and the trade-off of
giving the bot admin.

### 4.8 User experience (no-PR flow)

1. Issue with the trigger label → task → the agent edits → the harness
   commits and pushes to `agent/issue-<n>-<slug>`.
2. The bot comments on the issue with the summary, the cost and the link
   `{host}/{repo}/compare/{base}...{branch}` ("open the Pull Request here"),
   without `Closes #N`.
3. `issue_work` closes with `closed_reason="branch_published"`.
4. A maintainer comments on the issue (command) → the same branch is resumed,
   the harness merges `origin/<base>` and checks for conflicts locally, and
   the result comes back as a comment on the issue.
5. A human opens the PR through the UI, reviews it and merges it. **Never
   merge PRs** still applies.

---

## 5. Implementation phases

Each phase is one atomic PR, following `AGENTS.md`.

| Phase | Deliverable | Tests |
|---|---|---|
| **G1** — Settings + webhook | `settings.py`, `webhook.py`; JSON fixtures captured from a real Gogs (`opened`, `label_updated`, `label_cleared`, `closed`, `reopened`, `issue_comment` on an issue and on a PR, bot comment, no secret); measurement of the UUID on redelivery | `tests/unit/providers/gogs/test_webhook.py`, `test_settings.py` |
| **G2** — Client + transport | `client.py` (httpx), `transport.py` | `test_client.py` with `httpx.MockTransport`; `test_transport.py` (custom host, subpath) |
| **G3** — Registration + contract | `plugin.py`; entry in `BUILTIN_PROVIDER_TYPES` and in the discriminated union | Parametrized contract suite passes for `gogs` |
| **G4** — Opt-in E2E | `gogs/gogs` container (setup via the API: bot user, token, repo, webhook) + fake LLM | `tests/e2e/test_gogs_flow.py`: label → push → comment with compare link; follow-up resumes the branch |
| **G5** — Docs | `docs/operations.md` (Gogs setup, author modes, secrets), `config.yaml.example`, `README.md`, `AGENTS.md` | — |

---

## 6. Testing strategy

- **Unit:** real fixtures in `tests/unit/providers/gogs/fixtures/*.json` with
  the delivery headers. No network: `httpx.MockTransport` in the client.
- **Contract:** the same suite as `PRD.md` §5, including fail-closed without
  a secret, `pulls() is None` consistent with `supports_pr_api=False`, a
  well-formed `compare_url`, and "labels changed without a trigger label" ⇒
  no-op.
- **E2E (opt-in, `tests/e2e/`):** Gogs in a container. Validates that nothing
  outside `providers/gogs/` had to change (success criterion).
- **Test config:** `_SettingsNoEnv` subclass (project pattern) with a `gogs`
  instance.

---

## 7. Acceptance criteria

1. Contract suite green for `gogs`; `ruff check` and `uv run pytest` green.
2. `git diff` of the implementation PR with no changes to `queue/`,
   `worker.py`, `runner/`, `policy/` or `observability/`.
3. The opt-in e2e passes against a real Gogs: labeled issue ⇒ branch
   published ⇒ comment with a compare link; follow-up reuses the branch.
4. Webhook without a secret or with an invalid signature ⇒ 400, and the
   delivery is not marked as processed.
5. The bot's own comment and a comment on a PR do not create a task.
6. The runbook documents the bot setup, both author modes and the
   per-instance secrets.

---

## 8. Risks and mitigations

| # | Risk | Mitigation |
|---|---|---|
| 1 | Core prerequisites not delivered | This PRD only starts after Phases 1–3 (+ `terms`) of `PRD.md` |
| 2 | Without an automatic PR, the human has to open the PR | Compare link in the comment; explicit wording; no auto-close promise |
| 3 | Bot with admin just to check collaborators | Default `allowlist`; `api` is opt-in and documented |
| 4 | Redelivery with a new UUID ⇒ double execution | Measure in G1; if so, content-based `delivery_key` + the core's creation dedupe |
| 5 | Unknown comment size limit | Conservative `max_body_length`; truncation in the core |
| 6 | Payload changes between Gogs versions | Versioned real fixtures; e2e against an image with a pinned tag |
| 7 | Label mutation by ID fails if the label does not exist | Ignore with a log; never create labels in the user's repo |
| 8 | Spoofing via `X-Github-*` | Provider chosen by path; the Gogs adapter only trusts `X-Gogs-*` |

---

## 9. Open questions

1. Does Gogs's "Redeliver" reuse the `X-Gogs-Delivery`? (measure in G1)
2. What is the actual comment body limit? It depends on the column type in
   the Gogs database (SQLite/MySQL/Postgres).
3. What is the minimum supported Gogs version? Define it from the target
   instance and pin the e2e image tag.
4. Which repo instruction files to look for (`terms.repo_instructions_files`)?
   Suggestion: only the generic ones (`AGENTS.md`, `CLAUDE.md`), with no
   forge-specific path.
5. How to map instance ids with a hyphen to env (`gogs-corp` →
   `GOGS_CORP`)? A Phase 3 decision of `PRD.md`.
6. Is a future `ensure_webhook` (creating the webhook via the API) worth it?
   First, confirm the hooks routes in the v1 API of the target version.
