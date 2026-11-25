# PRD — Gitea Provider

**Status:** Draft, not scheduled. Its prerequisites — Phases 0–4 of the
[provider plugins PRD](./PRD.md), including the `terms` vocabulary, the
optional `PullRequestClient` port and per-instance settings — are done.
**Date:** 2026-11-04
**Type:** `feat` (new provider; no change to the core)
**Contributor guide:** [`docs/providers.md`](./docs/providers.md) — this PRD
follows its structure and checklist.

## Revision history

| Date | Change |
|---|---|
| 2026-11-04 | Original version. Gitea facts come from the Gitea API/webhook documentation and prior research (`PRD.md` §9, Appendix A); the ones marked **(verify)** have not been checked against a running instance and are measured in phase GT1 before the code depends on them. |
| 2026-11-25 | GT1 measurements against **Gitea 28.4.0 and 1.27.3** (`tests/unit/providers/gitea/fixtures/capture.py`, results in `fixtures/measurements/`). Gitea dropped the `1.` prefix with 28.0 (Sep 2026). **Q1:** "Replay" sends a **new** `X-Gitea-Delivery` with a byte-identical body ⇒ content-based `delivery_key` (§4.2). **Q2:** a label change is `X-Gitea-Event: issues`, `X-Gitea-Event-Type: issue_label`, `action: label_updated`/`label_cleared`; **`label_cleared` still carries the pre-clear labels** in `issue.labels` ⇒ the adapter forces an empty set. A comment on a PR is `issue_comment` / `pull_request_comment` and is delivered **only** with the hook's separate *Pull Request Comment* toggle. **Q3:** minimal token scopes are `write:issue` + `write:repository` + `read:organization`; `read:user` only for `GET /user`. **Q4:** a `write` collaborator gets 403 on another user's permission; repo admin gets it. **Q5:** both versions report a transient `mergeable=false` on a clean PR and a stale `true` right after a conflicting push ⇒ one read is not a verdict (§4.4). Also confirmed: `GET .../comments` ignores `limit`/`page`; `POST .../issues/{n}/labels` and `CreatePullRequestOption.labels` accept IDs, and the former also accepts names on both versions; `X-Gitea-Signature` is bare hex and is empty on a hook without a secret. Decisions: TLS via `ca_bundle_path` only, no `verify_tls` (§4.7, Q9); one `feat/gitea-provider` branch and PR for the whole PRD; `GiteaKit.make` and its `KITS` entry land with `plugin.py` in GT3. |

---

## 1. Vision and goals

Let fly-swe serve repositories hosted on a self-hosted **Gitea** server, with
the same end-to-end flow as GitHub: a labeled issue (or a command in a
comment) becomes a task, the agent fixes it, and the harness opens a **Pull
Request** linked to the issue for a human to review.

Unlike Gogs ([`PRD-gogs.md`](./PRD-gogs.md)), Gitea **has a PR API**, so this
is the first new provider to exercise the full `PullRequestClient` path
(create, find, comment, conflict check) on a forge other than GitHub.

### Goals

1. Implement `src/issue_agent/providers/gitea/` as a `ProviderPlugin`
   (`docs/providers.md` § The ports) registered in `BUILTIN_PROVIDER_TYPES`.
2. Implement `pulls(repo)` (`supports_pr_api=True`): the agent opens a PR,
   marked as draft with the `WIP: ` title prefix (Gitea has no native draft
   flag), labeled `agent-generated`, with `Closes #N` in the body.
3. Pass the conformance suite
   (`tests/unit/providers/test_provider_contract.py`) through a `GiteaKit`,
   and an opt-in e2e against a real Gitea in a container.

### Success criterion

> The Gitea plugin is added **without any change** outside
> `src/issue_agent/providers/gitea/`, `tests/**/providers/gitea/` and the type
> registration (entry in `BUILTIN_PROVIDER_TYPES`, member of the
> `providers.*` discriminated union in `providers/settings.py`, `GiteaKit` in
> `tests/unit/providers/kits.py`), plus documentation and config examples.
> If the implementation requires changing `queue/`, `worker.py`, `runner/`,
> `policy/`, `prompts/` or `observability/`, that is a gap in the ports: it is
> fixed generically in a separate PR (with an issue first, per
> `docs/providers.md` § The rule), before continuing here.

### Non-goals

- **Forgejo.** It is a hard fork with a compatible v1 API and also sends the
  `X-Gitea-*` headers, so this plugin will probably work against it — but we
  don't promise or test it here. A `forgejo` type (likely a thin subclass
  reading `X-Forgejo-*`) is a follow-up (§10).
- Triggering on Gitea PR events (`pull_request`, PR review comments): out of
  scope, as in the GitHub provider. Comments on PRs are normalized with
  `target_kind="pr"` and the core ignores them.
- Merging PRs. **Never merge PRs** still applies.
- Managing webhooks through the API (`ensure_webhook`): the operator
  configures the webhook on the repository or organization.
- Gitea Actions, packages, wiki, projects.
- OAuth2 apps. The bot authenticates with a personal access token (§4.6).

---

## 2. Prerequisites (in the core, all on `main`)

| Prerequisite | Where | Why Gitea needs it |
|---|---|---|
| Canonical model and `parse_event` → `TriggerEvent \| Ignored` | `core/models.py`, Phase 1 | The Gitea payload must not leak into the core |
| `label_added` = "the label set changed" | `PRD.md` §3.1 | Gitea sends `label_updated` with the current labels, no diff |
| `Author.association="unknown"` + check deferred to the worker | §3.1, `policy/guards.py` | Gitea payloads carry no `author_association` |
| Per-instance route `POST /webhooks/{instance_id}` | Phase 2a | Gitea also sends `X-GitHub-*`/`X-Gogs-*`; provider chosen by path |
| `PullRequestClient` port, `draft_mode="title_prefix"` | §3.2, §3.7 | Draft via `WIP: ` prefix |
| `providers.*` union, secrets in `PROVIDERS__<ID>__*` | Phase 3 | Per-instance config and credentials |
| `capabilities.terms` in prompts and core text | Phase 4 | Neutral vocabulary; no `.github/` instruction files |

---

## 3. Gitea facts

Target: Gitea 1.27 and 28 — Gitea dropped the `1.` prefix with 28.0
(September 2026), so 28 is the release after 1.27. Both were measured in
GT1; the minimum supported version is still an open question (§10). The
API lives at `{host}/api/v1` and is documented by the instance's own Swagger
(`{host}/api/swagger`).

### 3.1 v1 API

| Resource | Routes | Note |
|---|---|---|
| Repo | `GET /repos/{owner}/{repo}` | `default_branch`, `clone_url`, `html_url`, `full_name` |
| Issue | `GET /repos/{o}/{r}/issues/{index}` | `state`, `labels`, `html_url`, `pull_request` (non-null when it is a PR) |
| Issue comments | `GET, POST /repos/{o}/{r}/issues/{index}/comments` | `GET` takes `since`/`before` but **no `limit`/`page`** (measured: `limit=1` still returns every comment) — the client fetches and keeps the last `limit` |
| Edit comment | `PATCH /repos/{o}/{r}/issues/comments/{id}` | Available for a future `upsert_comment` |
| Issue labels | `POST /repos/{o}/{r}/issues/{index}/labels` `{"labels": [...]}` | Accepts names as well as IDs on 1.27 and 28 (measured); the plugin still resolves names → IDs, which `CreatePullRequestOption.labels` needs anyway, and to skip unknown names instead of failing the call |
| Repo labels | `GET /repos/{o}/{r}/labels` | Paginated (`page`, `limit`) |
| Org labels | `GET /orgs/{org}/labels` | Org-level labels apply to org repos; the name → ID map must include them |
| Pulls | `GET /repos/{o}/{r}/pulls?state=open`, `POST /repos/{o}/{r}/pulls`, `GET /repos/{o}/{r}/pulls/{index}` | `CreatePullRequestOption`: `head`, `base`, `title`, `body`, `labels` (**IDs**), `assignees`; **no `draft` field** |
| PR by branches | `GET /repos/{o}/{r}/pulls/{base}/{head}` | Newer versions only (not measured — nothing depends on it); `find_open_pr` uses the list endpoint so it works everywhere |
| Permission | `GET /repos/{o}/{r}/collaborators/{user}/permission` | `permission`: `owner`/`admin`/`write`/`read`/`none`. Querying **another** user needs repo admin (or site admin) — measured: a `write` collaborator gets 403, an admin gets the answer (a non-collaborator reads as `read` on a public repo) |
| Current user | `GET /user` | Bot login; used only by the opt-in e2e and diagnostics, never by `health_check` |

API authentication: `Authorization: token <access-token>`. Tokens are
**scoped** (Gitea ≥ 1.19). Measured minimal set:

| Scope | Needed for |
|---|---|
| `write:issue` | issue read, comments, issue labels, repo labels |
| `write:repository` | `GET /repos/{o}/{r}`, pulls (list/create), permission endpoint, git push over HTTP |
| `read:organization` | org labels (`GET /orgs/{org}/labels`) |
| `read:user` | only `GET /user` (opt-in e2e and diagnostics) — optional |

Indexes: issues and PRs **share one index per repo**, so a PR's comments go
through `/issues/{index}/comments` and `#N` can point at either.

### 3.2 Webhooks

- **Headers.** Gitea sends `X-Gitea-Event`, `X-Gitea-Event-Type`,
  `X-Gitea-Delivery` and `X-Gitea-Signature`, and **also** `X-GitHub-Event`,
  `X-GitHub-Delivery`, `X-Gogs-*` and `X-Hub-Signature(-256)` for
  compatibility. That is why the provider is chosen **by path**
  (`/webhooks/{instance_id}`); the adapter reads only `X-Gitea-*`.
- **Signature.** `X-Gitea-Signature` is the hex HMAC-SHA256 of the raw body
  with the webhook secret, **no `sha256=` prefix**. With no secret on the
  hook, the signature is empty — verification fails closed. (Both measured.)
- **Delivery id.** `X-Gitea-Delivery` is the UUID of the hook task. The UI's
  "Replay" creates a **new** hook task with a **new** UUID and a
  byte-identical body (measured on 1.27.3 and 28.4.0) — hence the
  content-based key in §4.2.
- **Event vs. event type.** `X-Gitea-Event` carries the coarse event
  (`issues`, `issue_comment`, `pull_request`, ...), and `X-Gitea-Event-Type`
  the fine one (`issue_label`, `issue_assign`, `pull_request_comment`, ...).
  Measured: a label change arrives as `X-Gitea-Event: issues`,
  `X-Gitea-Event-Type: issue_label`, `action: label_updated` (or
  `label_cleared` when every label is removed). The adapter maps on
  `(X-Gitea-Event, action)` and logs both.
- **Hook event toggles.** In the "custom events" UI, *Issues*, *Issue Label*
  and *Issue Comment* are separate checkboxes; the runbook tells the operator
  to enable all three. A comment on a PR is a fourth toggle, *Pull Request
  Comment*: without it Gitea does not deliver PR comments at all (measured),
  and with it they arrive as `X-Gitea-Event: issue_comment`,
  `X-Gitea-Event-Type: pull_request_comment`. The core ignores PR comments,
  so the runbook leaves it off; the adapter still marks them `target_kind="pr"`.
- **`IssuePayload`:** `action`, `number`, `changes`, `issue`, `repository`,
  `sender`. Actions include `opened`, `closed`, `reopened`, `edited`,
  `label_updated`, `label_cleared`, `assigned`, `milestoned`, ... **No label
  diff:** `issue.labels` is the current set — except on `label_cleared`,
  where it still lists the labels from **before** the clear (measured on
  both versions).
- **`IssueCommentPayload`:** `action` (`created`/`edited`/`deleted`),
  `issue`, `comment`, `changes`, `repository`, `sender`, `is_pull`. A comment
  on a PR comes as `X-Gitea-Event: issue_comment` with `is_pull: true` and
  `issue.pull_request` set.
- **`Issue`:** `id`, `number`, `user`, `title`, `body`, `labels[].name`,
  `state`, `html_url`, `pull_request`, `updated_at`.
- There is **no `author_association`**.

### 3.3 Drafts, auto-close and limits

- **Draft:** a PR is "work in progress" when its title starts with one of
  `[repository.pull-request] WORK_IN_PROGRESS_PREFIXES` in `app.ini`
  (default `WIP:,[WIP]`). A human removes the prefix to mark it ready.
- **Auto-close:** merging into the **default branch** closes issues
  referenced with one of `[repository] CLOSE_KEYWORDS` (default includes
  `close`, `closes`, `fix`, `fixes`, `resolve`, ...). An operator can change
  both lists per instance (§8, risk 4).
- **Body size:** issue/comment bodies are `LONGTEXT`-class columns with no
  documented API limit; a reverse proxy's request-size limit is the usual
  bound (not measured in GT1; the conservative
  `max_body_length` in §4.1 stands, see §10 Q7).

### 3.4 Ecosystem

- There is no maintained Python SDK for Gitea (`giteapy` is stalled since
  2020 — rejected in `PRD.md` Appendix A; `pyforgejo` targets Forgejo). The
  plugin uses **`httpx`**, already a direct dependency — no new dependency,
  no optional extra.
- `ogr`'s Forgejo backend was considered in `PRD.md` Appendix A; it does not
  cover webhooks or drafts and pulls heavy deps. Not used.

---

## 4. Plugin architecture

```text
providers/gitea/
├── __init__.py
├── plugin.py      → GiteaProviderPlugin (type="gitea", capabilities, from_settings,
│                    bot_identity, client, pulls, transport, health_check)
├── settings.py    → GiteaProviderSettings (member of the providers.* union; no httpx import)
├── webhook.py     → GiteaWebhookAdapter (delivery_key, verify_signature, event_name, parse_event)
├── normalize.py   → payload → IssueRef/Author/Comment (shared by webhook and client)
├── api.py         → thin httpx wrapper: base URL, token header, timeouts, pagination, error mapping
├── client.py      → GiteaForgeClient
├── pr.py          → GiteaPullRequestClient
└── transport.py   → GiteaGitTransport
```

The `httpx.Client` is built **lazily** on first outbound use (the web process
only needs `webhook`), as `docs/providers.md` recommends.

### 4.1 Capabilities

| Field | Value | Reason |
|---|---|---|
| `draft_mode` | `"title_prefix"` | No draft field in `CreatePullRequestOption` |
| `draft_prefix` | `"WIP: "` | Matches Gitea's default `WIP:` prefix |
| `supports_pr_api` | `True` | `pulls()` is overridden |
| `supports_labels` | `True` | Issue and PR labels via the API (by ID) |
| `auto_close_keyword` | `"Closes"` | In Gitea's default `CLOSE_KEYWORDS` |
| `max_body_length` | `65_000` | Conservative (§3.3, §10); truncation happens in the core |
| `terms.issue` / `terms.pr` | `"issue"` / `"Pull Request"` | Gitea UI vocabulary |
| `terms.ref_prefix_issue` / `ref_prefix_pr` | `"#"` / `"#"` | Shared index |
| `terms.repo_instructions_files` | `[".gitea/copilot-instructions.md"]`? | Open question (§10); default to `[]` |

### 4.2 `WebhookAdapter`

- **`verify_signature`:** requires a configured `webhook_secret` **and** a
  non-empty `X-Gitea-Signature`; compares
  `hmac.new(secret, body, sha256).hexdigest()` with `hmac.compare_digest`
  (lower-cased). Anything missing ⇒ `WebhookRejectedError` with
  `mark_reason=None`. `X-Hub-Signature-256` is ignored.
- **`delivery_key`:** content-based, since "Replay" issues a new UUID with
  the same body (§3.2): `sha256(X-Gitea-Event + X-Gitea-Event-Type + raw
  body)`, so a replayed delivery is a duplicate. Hashing the raw bytes needs
  no parsing, so the key is computed before the signature check without
  raising on a malformed body. Two distinct events always differ in the body
  (`issue.updated_at`, labels, `comment.id`); two identical bodies describe
  the same state, so collapsing them is harmless. `X-Gitea-Delivery` is
  still required — missing ⇒ `WebhookRejectedError` (no `mark_reason`). The
  key is `gitea-<hex>`; the dispatcher logs it as the delivery id, and
  `event_name` shows both event headers. The choice is recorded in the
  module docstring.
- **`event_name`:** `X-Gitea-Event`, with `/X-Gitea-Event-Type` appended
  when different (`issues/issue_label`, `issue_comment/pull_request_comment`)
  — it is what the dispatcher logs and records.
- **`parse_event`:** normalizes only; `policy/ingestion.py` decides.

| `X-Gitea-Event` / `action` | Result | Normalization |
|---|---|---|
| `issues` / `opened` | `TriggerEvent(kind=issue_opened)` | — |
| `issues` / `label_updated`, `label_cleared` | `TriggerEvent(kind=label_added)` | `IssueRef.labels` = current set; **forced empty on `label_cleared`**, whose payload still lists the old labels (§3.2); the core decides from the set |
| `issues` / `closed`, `reopened` | `issue_closed`, `issue_reopened` | `state` from `issue.state` |
| `issue_comment` / `created` | `TriggerEvent(kind=comment_command)` | `target_kind="pr"` when `is_pull` or `issue.pull_request`; `trigger_comment` filled |
| `issues` / other actions, `issue_comment` / `edited`, `deleted` | `Ignored(reason=..., repo=...)` | Unsupported action |
| any other event (`push`, `pull_request`, ...) | `Ignored(reason=..., repo=...)` | Unsupported event |
| invalid JSON | `WebhookRejectedError(mark_reason="invalid_json")` | Authentic but unusable |
| not an object / missing `issue` or `repository` | `WebhookRejectedError(mark_reason="invalid_payload")` | Same reasons as the GitHub adapter |

  - `Author.association = "unknown"`; `Author.is_self = (login.lower() ==
    bot_login.lower())`; a `sender.type`/`is_bot` hint is not relied upon.
  - `repo = repository.full_name` (opaque slug).
  - `IssueRef.url = issue.html_url`; `IssueRef.provider_instance =
    instance_id`; `global_id = str(issue.id)`.
  - `credentials_ref = None` (one token per instance).

### 4.3 `ForgeClient`

| Method | Implementation |
|---|---|
| `get_issue_state(n)` | `GET /repos/{repo}/issues/{n}` → `state` |
| `list_comments(n, limit)` | `GET /repos/{repo}/issues/{n}/comments` → last `limit`, oldest first, `association="unknown"`, `is_self` by login |
| `post_comment(n, body)` | `POST /repos/{repo}/issues/{n}/comments` → `html_url` |
| `get_default_branch()` | `GET /repos/{repo}` → `default_branch` |
| `add_labels(n, labels)` | names → IDs from repo + org labels (cached per client) → `POST .../issues/{n}/labels`; an unknown name is logged and skipped — **never create labels** in the user's repo |
| `author_permission(login)` | `mode=api`: `GET .../collaborators/{login}/permission` → `owner`→`"owner"`, `admin`/`write`→`"collaborator"`, `read`/`none`→`"none"`; 403 ⇒ `"unknown"` + warning; cached per `(repo, login)`. `mode=allowlist` (default): `"unknown"` |

HTTP errors (`api.py`): 401/403 ⇒ credential/permission error, no retry;
404 ⇒ missing resource; 5xx and timeouts ⇒ recoverable under the worker's
existing retry policy. The token is never logged; request logs carry method,
path and status only.

### 4.4 `PullRequestClient`

| Method | Implementation |
|---|---|
| `find_open_pr(branch)` | `GET /repos/{repo}/pulls?state=open` (paginated) → the one with `head.ref == branch` and `head.repo.full_name == repo` (same-repo branches only) → `PRRef(number, html_url, head.ref)` |
| `create_pr(head, base, title, body, draft, labels)` | title = `"WIP: " + title` when `draft`; label names → IDs (as in `add_labels`; missing ⇒ skipped with a log); `POST /repos/{repo}/pulls` → `PRRef` |
| `comment_on_pr(pr, body)` | `POST /repos/{repo}/issues/{pr.number}/comments` → `html_url` |
| `pr_has_conflicts(pr, head_sha)` | `GET /repos/{repo}/pulls/{n}`; `None` unless `head.sha == head_sha`. Gitea computes mergeability in the background and the API exposes no "checking" state. Measured on both versions: a clean PR read `mergeable=false` for up to ~1.5 s after creation, and a PR read a stale `true` for ~0.2 s after a conflicting push. So **one read is not a verdict**: GT3 returns `True` only when `mergeable is False` holds across reads spread over a few seconds, `False` when `True` holds likewise, and `None` otherwise (the core acts only on a definite `True` and falls back to `git merge-tree`) |

### 4.5 `GitTransport`

| Method | Value |
|---|---|
| `clone_url(repo)` | `{host}/{repo}.git` — no credential |
| `git_auth_env(repo)` | `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=http.{host}/.extraHeader`, `GIT_CONFIG_VALUE_0=Authorization: Basic base64("<bot_login>:<token>")`, `GIT_TERMINAL_PROMPT=0` — same technique as `providers/github/transport.py`; the token is never in the URL or on disk |
| `pr_url(repo, n)` | `{host}/{repo}/pulls/{n}` |
| `compare_url(repo, base, head)` | `{host}/{repo}/compare/{base}...{head}` (branch names URL-quoted) |
| `format_ref(kind, n, repo, from_repo)` | `#n` when `repo == from_repo`, else `owner/repo#n` (same for issues and PRs) |
| `closing_reference(issue_repo, n, target_repo)` | `Closes #n`, or `Closes owner/repo#n` across repos — Gitea resolves cross-repo keywords on the same instance |
| `default_commit_email(name)` | `{name}@noreply.{hostname}` (Gitea's default `NO_REPLY_ADDRESS` domain; Gitea itself renders a user's private address as `{id}+{login}@noreply.{hostname}`, observed in GT1 payloads); `git_identity.user_email` overrides it |

`host` may carry a subpath (`https://example.com/git`); every URL above is
built from it, and the `extraHeader` key uses it as the prefix.

### 4.6 Identity and credentials

- A **bot user account** on Gitea (a regular user; Gitea has no app
  installation model), added as a collaborator with **write** access on the
  issues and target repos — or to an org team with write access. Gitea 28
  adds dedicated **bot accounts** (token-only, no interactive login, no
  notifications), the better fit there; the runbook (GT5) documents both.
- A **scoped access token** for that account (§3.1). It does not expire
  unless the operator sets an expiry; the client does not refresh it. 401
  becomes a terminal failure with an alert.
- `bot_identity()` returns `Author(login=bot_login, association="bot",
  is_self=True)`. `bot_login` is **required**: without it the own-comment
  filter can't work, and `health_check` reports it missing.

### 4.7 Settings

```python
# src/issue_agent/providers/gitea/settings.py
class GiteaProviderSettings(ProviderSettingsBase):
    secret_fields: ClassVar[frozenset[str]] = frozenset({"token", "webhook_secret"})

    type: Literal["gitea"]
    host: str = ""                              # required; scheme required, trailing slash stripped
    bot_login: str = ""                         # required
    author_permission: Literal["allowlist", "api"] = "allowlist"
    ca_bundle_path: str = ""                    # self-hosted instances with a private CA
    request_timeout_seconds: float = 30.0
    token: str = ""
    webhook_secret: str = ""

    @property
    def api_url(self) -> str:
        return f"{self.host}/api/v1"
```

- `host` is validated like the GitHub one (scheme required, trailing slash
  dropped). There is no sensible default for a self-hosted forge.
- `ca_bundle_path` (non-secret, e.g. a PEM on the volume) is the CA bundle
  the HTTP client verifies the instance against; empty ⇒ the system store.
  There is deliberately **no** switch to disable TLS verification (§10 Q9).
  `health_check` reports a bundle path that doesn't exist.
- The module imports only pydantic and `providers.base` — no `httpx`.

Example (`config.yaml.example`):

```yaml
providers:
  gitea_corp:                          # → POST /webhooks/gitea_corp
                                       # (no '-': env can't name it, #26)
    type: gitea
    host: https://git.example.com      # API at {host}/api/v1
    bot_login: issue-agent
    author_permission: allowlist       # allowlist | api (api needs bot repo admin)
repos:
  - issues: team/app
    target: team/app
    provider: gitea_corp
```

Secrets, env only (`.env.example`, `docs/operations.md` § Configuration):
`PROVIDERS__GITEA_CORP__TOKEN`, `PROVIDERS__GITEA_CORP__WEBHOOK_SECRET`.

### 4.8 `health_check()`

From config only, no network, never raises:

- `config`: `"ok"` or `"missing: providers.<id>.host, providers.<id>.bot_login,
  PROVIDERS__<ID>__TOKEN, PROVIDERS__<ID>__WEBHOOK_SECRET"` (only the missing
  ones).
- `tls`: `"ok"`, or `"missing: providers.<id>.ca_bundle_path (<path>)"` when
  the configured bundle doesn't exist (a local file check, no network).
- `author_permission`: `"warning: api mode needs the bot to be repo admin"`
  when `mode=api` (informational).

### 4.9 Comment-author policy

Same model as Gogs (`PRD-gogs.md` §4.7). With `association="unknown"`:

- **`allowlist` (default):** the issue author and
  `comments_author_filter_allowlist` are accepted; nothing needs bot admin.
- **`api`:** the worker calls `author_permission` (`policy/guards.py`);
  `"collaborator"`/`"owner"` count when listed in
  `comments_author_filter_allow_associations`. Costs one API call per
  unknown commenter (cached) and requires repo admin for the bot.

The runbook documents both modes and the trade-off of granting admin.

### 4.10 User experience

1. Issue gets the trigger label (or a maintainer comments a command) → task.
2. The agent edits; the harness commits, pushes `agent/issue-<n>-<slug>` and
   opens a PR titled `WIP: <title>` with `Closes #<n>`, labeled
   `agent-generated` (if the label exists in the repo or org).
3. The bot comments on the issue with the summary, the cost and the PR link.
4. A follow-up command on the issue resumes the same branch; the harness
   updates the PR and comments on it; conflicts come from
   `pr_has_conflicts` (or `git merge-tree` in the core when it returns
   `None`).
5. A human removes `WIP:`, reviews and merges. Merging into the default
   branch closes the issue.

---

## 5. Implementation phases

All phases land on **one** `feat/gitea-provider` branch and one PR, each
phase as a block of atomic commits, following `AGENTS.md` and
`CONTRIBUTING.md` (Conventional Commits, ruff + pytest green on every
commit, coverage ≥ the floor). The minor version bump (`uv lock` after) is
one commit at the end.

| Phase | Deliverable | Tests |
|---|---|---|
| **GT1** — Settings + webhook + kit | `settings.py`, `webhook.py`, `normalize.py`; real fixtures captured from a Gitea container (§6); measurement of every **(verify)** item in §3 — delivery UUID on replay, label-event headers, token scopes, permission endpoint rights, mergeability while checking — recorded in this PRD's revision history; settings union entry; `GiteaKit.delivery` (its `make` needs the plugin, so it lands in GT3) | `test_settings.py`, `test_normalize.py`, `test_webhook.py`, `test_signature.py`, `test_kit.py` (the contract's webhook checks against the adapter until GT3) |
| **GT2** — Client + transport | `api.py`, `client.py`, `transport.py` | `test_client.py` with `httpx.MockTransport` (pagination, org labels, 401/404/5xx, permission mapping); `test_transport.py` (custom host, subpath, cross-repo refs) |
| **GT3** — PRs + registration | `pr.py`, `plugin.py`; entry in `BUILTIN_PROVIDER_TYPES`; `GiteaKit.make` with an injected `httpx.MockTransport` and its `KITS` entry (`test_kit.py` goes) | `test_pr_ops.py` (WIP prefix, label IDs, find by head, conflicts `None`/`True`/`False`), `test_plugin.py` (health checks); conformance suite green for `gitea` |
| **GT4** — Opt-in e2e | `gitea/gitea:<pinned>` container, set up via the API (admin, bot user, scoped token, repo, labels, webhook) + fake LLM | `tests/e2e/test_gitea_flow.py`: label → PR `WIP:` with `Closes #N` → comment on the issue; follow-up command reuses the branch and PR |
| **GT5** — Docs | `docs/operations.md` (Gitea setup: bot user, token scopes, webhook events, author modes, secrets), `config.yaml.example`, `.env.example`, `README.md`, `AGENTS.md` (Provider plugins), `PRD.md` §9 (point to this PRD) | — |

GT1 can be opened early as a draft PR — the conformance suite is the
progress bar (`docs/providers.md` § Opening the PR).

---

## 6. Testing strategy

- **Conformance suite.** `GiteaKit` in `tests/unit/providers/kits.py`,
  registered in `KITS` (`test_every_builtin_type_has_a_kit` fails until it
  is):
  - `type = "gitea"`, `reports_association = False`.
  - `make(instance_id, secret)` builds the plugin with a fake `token` and an
    injected `httpx.MockTransport`, so the outbound ports work offline.
  - `delivery(sample, *, secret, delivery_key)` returns real Gitea wire
    format — `X-Gitea-Event`, `X-Gitea-Event-Type`, `X-Gitea-Delivery`
    (omitted when `delivery_key is None`), `X-Gitea-Signature` — for each
    `SAMPLES` entry:

    | Sample | Gitea fixture |
    |---|---|
    | `issue_opened` | `issues` / `opened` |
    | `comment` | `issue_comment` / `created` on an issue |
    | `comment_on_pr` | `issue_comment` / `created` with `is_pull: true` |
    | `own_bot_comment` | `issue_comment` / `created` by `bot_login` |
    | `labels_changed_without_trigger` | `issues` / `label_updated` without the trigger label |
    | `unsupported` | `push` |

    If the content-based `delivery_key` is adopted (§4.2), the kit still
    exercises "missing key" by omitting `X-Gitea-Delivery` — the adapter
    rejects a delivery without it even when the key is derived from content.
- **Fixtures.** Real payloads under
  `tests/unit/providers/gitea/fixtures/*.json`, captured by `capture.py`
  from a `gitea/gitea:28.4.0` container seeded with synthetic data (nothing
  personal to trim), with the version and the regeneration command in a
  `README` next to them.
- **Unit.** `tests/unit/providers/gitea/` mirrors the GitHub layout. No
  network: `httpx.MockTransport`.
- **Config.** Production-like tests use a settings subclass that nullifies
  `env_file`/`yaml_file` (project rule), with a `gitea` instance.
- **E2E (opt-in, `tests/e2e/`).** Real Gitea in a container; also proves the
  success criterion (no core change).
- **What we don't test.** The core is already covered against
  `FakeProvider` with and without a PR API and association.

---

## 7. Acceptance criteria

1. Conformance suite green for `gitea`; `uv run ruff check`, `uv run ruff
   format --check` and `uv run pytest` green; coverage ≥ the floor.
2. The implementation PRs' `git diff` touches nothing in `queue/`,
   `worker.py`, `runner/`, `policy/`, `prompts/` or `observability/`.
3. The opt-in e2e passes against a real Gitea: labeled issue ⇒ `WIP:` PR
   with `Closes #N` and the `agent-generated` label ⇒ comment on the issue;
   follow-up reuses the branch and PR.
4. A webhook with no secret, an empty or invalid signature, or no delivery
   id ⇒ 400, and the delivery is not marked as processed.
5. The bot's own comment, a comment on a PR and a label change without a
   trigger label do not create a task.
6. The runbook documents the bot setup, token scopes, the three webhook
   event toggles, both author modes and the per-instance secrets.
7. The security checklist below is ticked in the PR description.

### Security checklist (`docs/providers.md`)

- [ ] `X-Gitea-Signature` checked with `hmac.compare_digest`; empty secret or
      empty signature ⇒ reject.
- [ ] No rejection before signature verification sets `mark_reason`.
- [ ] `token` and `webhook_secret` in `secret_fields`; never logged (request
      logs carry no headers).
- [ ] `clone_url` has no credential; `git_auth_env` scopes the header to
      `http.{host}/.extraHeader` and sets `GIT_TERMINAL_PROMPT=0`.
- [ ] The token lives inside the clients and is never returned to the core.
- [ ] The payload is untrusted: no templating of issue text; bodies
      truncated by the core at `max_body_length`; JSON parsing errors are
      `WebhookRejectedError`, not 500s.
- [ ] `health_check()` makes no network call.
- [ ] Only `X-Gitea-*` headers are trusted; `X-GitHub-*`/`X-Gogs-*`/
      `X-Hub-Signature*` are ignored.

---

## 8. Risks and mitigations

| # | Risk | Mitigation |
|---|---|---|
| 1 | "Replay" issues a new delivery UUID ⇒ double execution | Measure in GT1; if so, content-based `delivery_key` plus the core's creation dedupe |
| 2 | Spoofing via `X-GitHub-*`/`X-Hub-Signature-256` | Provider chosen by path; adapter reads only `X-Gitea-*` |
| 3 | Labels by ID, and labels may live at org level | Name → ID map from repo + org labels; missing label ⇒ log and skip; never create labels |
| 4 | Operator changed `WORK_IN_PROGRESS_PREFIXES` or `CLOSE_KEYWORDS` in `app.ini` | Document the defaults we rely on; if demand appears, make `draft_prefix`/`auto_close_keyword` configurable — that needs a port change (capabilities are class-level), so it goes through an issue first |
| 5 | `mergeable=false` while Gitea is still checking ⇒ false conflict (**confirmed** on 1.27.3 and 28.4.0, along with a stale `true`) | Never trust one read: require agreeing reads over a few seconds, else `None` (§4.4); core falls back to `git merge-tree` |
| 6 | Bot needs repo admin for `api` author mode | `allowlist` is the default; `api` is opt-in and documented |
| 7 | Payload and API drift between Gitea versions | Versioned fixtures; pinned e2e image; minimum version documented |
| 8 | Self-signed TLS on self-hosted instances | `ca_bundle_path`; no switch to disable verification (§4.7) |
| 9 | Over-scoped token | Document the minimal scopes; one token per instance, env only |
| 10 | Forgejo users try the `gitea` type | Not promised (non-goal); note in the runbook; `forgejo` type tracked as follow-up |

---

## 9. Out of scope, recorded for later

- `forgejo` type (`X-Forgejo-*` headers, `X-Forgejo-Delivery`, otherwise the
  same plugin).
- `upsert_comment` via `PATCH /issues/comments/{id}` (Renovate-style
  `ensureComment`), once the port grows it.
- `ensure_webhook` through `POST /repos/{o}/{r}/hooks`.
- Triggers on PR events and PR review comments.

---

## 10. Open questions

1. ~~Does Gitea's "Replay" reuse `X-Gitea-Delivery`?~~ **No** — new UUID,
   same body; content-based key (§4.2). (GT1)
2. ~~Exact headers and `action` for a label change?~~ **Yes** as guessed:
   `issues` / `issue_label` / `label_updated` (and `label_cleared`, with
   stale labels). (GT1)
3. ~~Minimal token scope set?~~ `write:issue`, `write:repository`,
   `read:organization` (+ `read:user` for `GET /user` only) (§3.1). (GT1)
4. ~~Can a write-only bot query others' permission?~~ **No** (403); repo
   admin is required. (GT1)
5. ~~Does `mergeable` read `false` while pending?~~ **Yes**, and it can read
   a stale `true` after a conflicting push (§4.4). (GT1)
6. Minimum supported Gitea version, and the e2e image tag to pin. GT1
   measured 1.27.3 and 28.4.0 with identical results except `mergeable`
   timing (28 adds a `type` field to user objects). Proposal: support 1.27+,
   pin `gitea/gitea:28.4.0` for the e2e.
7. `max_body_length`: keep `65_000` or raise toward the `~1,000,000` the
   providers PRD noted? Depends on DB column types and typical proxy limits.
8. `terms.repo_instructions_files`: `[]` (generic `AGENTS.md`/`CLAUDE.md`
   only) or also `.gitea/copilot-instructions.md`? Proposal: `[]` — Gitea
   has no such convention.
9. ~~`verify_tls: bool` vs. a `ca_bundle_path` setting?~~ **Decided:**
   `ca_bundle_path` only (§4.7).
10. Should the `gitea` type accept Forgejo explicitly (reading
    `X-Forgejo-*` as an alias), or wait for a separate `forgejo` type?
