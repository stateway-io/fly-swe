# Writing a provider plugin

fly-swe talks to a forge — GitHub today; GitLab, Gitea/Forgejo or Gogs
tomorrow — only through a **provider plugin**. This guide is for anyone who
wants to add one. It covers the ports a plugin implements, the canonical
model it produces, how it is configured and registered, and the conformance
suite that decides whether it is done.

The design record behind all of this is the [provider plugin PRD](../PRD.md);
the code is the authoritative reference, starting with
[`src/issue_agent/providers/base.py`](../src/issue_agent/providers/base.py).
Two working examples to read alongside this guide:

- [`providers/github/`](../src/issue_agent/providers/github/) — the real
  GitHub App plugin.
- [`tests/fakes/fake_provider.py`](../tests/fakes/fake_provider.py) — an
  in-memory forge in under 400 lines, deliberately unlike GitHub (its own headers,
  its own JSON, no PR API). It is the shortest complete plugin in the repo.

## The rule

**Nothing outside `src/issue_agent/providers/` imports a forge SDK or names a
forge.** The queue, the label router, the runner and the prompts only see
canonical objects and the ports below. If adding your forge seems to need a
change in `runner/`, `worker.py` or `prompts/`, open an issue first — that is
a gap in the ports, and it should be fixed generically, not with an
`if provider == ...`.

## How a plugin is used

```text
POST /webhooks/<instance_id>
  └─ registry.get(instance_id).webhook          (unknown id → 404)
       ├─ delivery_key(headers, body)            idempotency key
       ├─ verify_signature(headers, body)        authentic or rejected
       ├─ (already processed? → "duplicate")
       └─ parse_event(headers, body)             → TriggerEvent | Ignored
  └─ policy/ingestion.py                         core decides: drop or enqueue
  └─ SQLite queue

worker
  └─ plugin.client(repo)       ForgeClient       issues, comments, labels
  └─ plugin.pulls(repo)        PullRequestClient (or None → publish branch)
  └─ plugin.transport()        GitTransport      clone/push auth, URLs, refs
```

The provider is chosen by the URL path, **never by sniffing headers** —
Gitea and Forgejo also send `X-GitHub-Event`, so header detection would mix
forges up.

## The canonical model

`parse_event` turns a forge payload into the models in
[`core/models.py`](../src/issue_agent/core/models.py). Nothing downstream
ever reads the raw payload again.

| Model | What it carries |
|---|---|
| `TriggerEvent` | `kind` (`TriggerKind`), `target_kind` (`"issue"` or `"pr"`), `issue`, `actor`, optional `trigger_comment`, `delivery_key`, optional `credentials_ref` |
| `TriggerKind` | `issue_opened`, `label_added`, `comment_command`, `issue_closed`, `issue_reopened` |
| `IssueRef` | `provider_instance`, `repo`, `number`, `title`, `body`, `labels`, `author`, `url`, `state`, optional `global_id` |
| `Author` | `login`, `association` (`owner`/`member`/`collaborator`/`contributor`/`none`/`bot`/`unknown`), `is_self` |
| `Comment` | `id`, `author`, `body`, `created_at` |
| `PRRef` | `number`, `url`, `head_ref` |
| `Ignored` | `reason` (reported in the webhook response), `repo` (or `None`) |

Rules of thumb:

- **Normalize, don't decide.** Whether an event deserves a task — a comment
  on a PR, the agent's own comment, a label change with no trigger label, a
  closed issue, an author not allowed to command the agent — is decided once,
  for every forge, by [`policy/ingestion.py`](../src/issue_agent/policy/ingestion.py).
  Your job is to fill `target_kind`, `is_self`, `labels`, `state` and
  `association` correctly. Return `Ignored` only for events you can't
  represent (an unsupported event type or action).
- **`repo` is an opaque slug.** Use whatever the forge calls it
  (`owner/name`, `group/sub/project`). The core never splits it.
- **`label_added` means "the label set changed".** Put the *current* labels
  in `IssueRef.labels`; the core decides from the whole set, so a forge that
  can't tell you which label was added still works.
- **Unknown association is fine.** If the webhook doesn't say how the author
  relates to the repo, use `association="unknown"`. The core then defers the
  check to the worker, which calls `ForgeClient.author_permission(login)`.
- **`credentials_ref`** is opaque to the core and handed back to you; GitHub
  stores the App installation id there. Leave it `None` if you don't need it.

## The ports

All of them live in [`providers/base.py`](../src/issue_agent/providers/base.py).

### `ProviderPlugin`

One configured instance of your forge type.

| Member | Contract |
|---|---|
| `type: ClassVar[str]` | The type name used in config (`type: gitlab`). |
| `capabilities: ClassVar[ProviderCapabilities]` | Static facts about the forge — see [Capabilities and terms](#capabilities-and-terms). |
| `instance_id`, `webhook` | Set in `__init__`; `webhook` is your `WebhookAdapter`. |
| `from_settings(instance_id, config, settings)` | Build the instance from its typed `providers.<id>` entry. `settings` is the root config, for what belongs to the agent rather than the forge (`agent_name`, `pr_label_agent_generated`). |
| `bot_identity()` | The service's own account, as an `Author` with `is_self=True`. |
| `client(repo)` | A `ForgeClient` bound to `repo`. |
| `pulls(repo)` | A `PullRequestClient`, or the inherited `None`. Override it **exactly** when `capabilities.supports_pr_api` is true — the registry refuses a plugin where the two disagree. |
| `transport()` | Your `GitTransport`. |
| `health_check()` | A `ProviderHealth(ok, checks)` from config alone. **Must not raise and must not call the network**; it backs `/readyz`. A check named `config` holds `"ok"` or `"missing: a, b"` with the real config keys or env vars (e.g. `PROVIDERS__GITLAB__WEBHOOK_SECRET`) and joins `/readyz`'s `settings` list; any other check shows up as `<instance_id>_<name>`. Informational `"warning: ..."` checks must not flip `ok`. |

Build expensive things (API clients, token caches) lazily on first outbound
use, as the GitHub plugin does: the web process only ever needs `webhook`.

### `WebhookAdapter`

| Method | Contract |
|---|---|
| `delivery_key(headers, body)` | A key that stays the same across redeliveries of one event (the forge's delivery id, or a hash of the content if the forge regenerates ids). |
| `verify_signature(headers, body)` | Raise `WebhookRejectedError` unless authentic. **Fail closed:** with no secret configured, nothing is authentic. Compare with `hmac.compare_digest`, never `==`. |
| `event_name(headers)` | The forge's event name, for logs. |
| `parse_event(headers, body)` | Return `ParsedDelivery(event, action, result)` where `result` is a `TriggerEvent` or `Ignored`; raise `WebhookRejectedError` on a malformed body. |

`WebhookRejectedError(error, *, log_event, mark_reason=None, status=400)`:
`error` goes in the response, `log_event` in the log. Set `mark_reason` only
for a delivery that should be recorded as processed (a malformed but
authentic body); a rejection before the signature is verified must leave it
`None`, so an unauthenticated request can never burn a delivery key.

### `ForgeClient`

Issue operations on one repo. Credentials are resolved — and renewed —
inside the client; the core never sees a token.

`get_issue_state(number)`, `list_comments(number, limit)` (most recent
`limit`, oldest first, `author.is_self` set on the bot's own),
`post_comment(number, body)` (returns the comment URL),
`get_default_branch()`, `add_labels(number, labels)`, and
`author_permission(login)` (defaults to `"unknown"`, which grants nothing —
override it if your webhook reports `unknown` associations).

### `PullRequestClient` (optional)

Only for a forge with a PR/MR API. `find_open_pr(branch)`,
`create_pr(*, head, base, title, body, draft, labels)` (apply `draft` as
your `draft_mode` allows), `comment_on_pr(pr, body)`, and
`pr_has_conflicts(pr, head_sha)` — return `None` when the forge can't tell
yet; the core only acts on a definite `True`.

Without this port the core still works end to end: it pushes the work
branch, comments on the issue with `transport().compare_url(...)` so a human
can open the PR by hand, checks conflicts locally with `git merge-tree`, and
reopens the same branch on follow-up comments (PRD §3.7).

### `GitTransport`

| Method | Contract |
|---|---|
| `clone_url(repo)` | The remote URL. **Never** embeds a credential — the agent can read `.git/config`. |
| `git_auth_env(repo)` | Env vars that authenticate one git process with a fresh credential, without writing it to disk. GitHub uses `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0` to set an `http.<host>.extraHeader` scoped to the forge host, plus `GIT_TERMINAL_PROMPT=0`. The harness passes these only to its own git commands, never to the agent's shell. |
| `pr_url(repo, number)`, `compare_url(repo, base, head)` | Web URLs. |
| `format_ref(kind, number, *, repo, from_repo)` | How text in `from_repo` refers to `repo`'s issue/PR: `#12`, `!12`, `owner/repo#12`. |
| `closing_reference(issue_repo, number, target_repo)` | The PR-body line that closes the issue on merge (`Closes #12`), or `None` if the forge can't be trusted to. |
| `default_commit_email(name)` | Used when `git_identity.user_email` is unset; defaults to a `.invalid` no-reply address. |

## Capabilities and terms

`ProviderCapabilities` is a class-level constant on your plugin:

| Field | Meaning | GitHub |
|---|---|---|
| `draft_mode` | `native`, `title_prefix` or `none` | `native` |
| `draft_prefix` | Title prefix when `title_prefix` (`"Draft: "`, `"WIP: "`) | — |
| `supports_pr_api` | Whether `pulls()` is implemented | `True` |
| `supports_labels` | Whether labels can be read and applied | `True` |
| `auto_close_keyword` | Keyword that closes the issue on merge, or `None` (the PR text then doesn't promise it) | `"Closes"` |
| `max_body_length` | Bodies above this are truncated by the core | `58_000` |
| `terms` | The forge's vocabulary (below) | |

`ProviderTerms` is what the prompts and the text the core writes are
rendered with:

| Field | GitHub | A GitLab plugin would use |
|---|---|---|
| `issue` | `issue` | `issue` |
| `pr` | `Pull Request` | `Merge Request` |
| `ref_prefix_issue` | `#` | `#` |
| `ref_prefix_pr` | `#` | `!` |
| `repo_instructions_files` | `[".github/copilot-instructions.md"]` | `[]` |

The prompts use `{{ terms.issue }}`, `{{ terms.pr }}` and
`{{ instructions_files }}` instead of forge words, and
`tests/unit/test_prompts.py` enforces it — see
[operations.md § Prompt vocabulary per provider](./operations.md#prompt-vocabulary-per-provider).
The values come from your static capabilities, never from a payload, so they
can't be used for prompt injection.

## Settings

Each type has a settings model, one entry of the discriminated union that
validates `providers.<id>`. Subclass `ProviderSettingsBase` (which sets
`extra="forbid"`, so typos fail loudly):

```python
# src/issue_agent/providers/gitlab/settings.py
from typing import ClassVar, Literal

from issue_agent.providers.base import ProviderSettingsBase


class GitLabProviderSettings(ProviderSettingsBase):
    secret_fields: ClassVar[frozenset[str]] = frozenset({"token", "webhook_secret"})

    type: Literal["gitlab"]
    host: str = "https://gitlab.com"
    bot_login: str = ""
    token: str = ""
    webhook_secret: str = ""
```

- **Non-secret fields** come from `config.yaml` (`providers.<id>.<field>`)
  or env (`PROVIDERS__<ID>__<FIELD>`); env wins, field by field.
- **`secret_fields`** come from env only. The loader rejects them in
  `config.yaml` with a message naming the env var to use.
- `<ID>` is the instance id upper-cased. Ids are lowercase letters and
  digits joined by single `-` or `_`, since they appear in the webhook path
  and `__` is the env delimiter.
- **Keep this module free of your forge's SDK.** It is imported every time
  the config loads; import the SDK only in the modules that make API calls.
- Validate and normalize in the model (the GitHub settings require a scheme
  on `host` and strip the trailing slash) so the plugin can trust its input.

## Registering the type

1. **Package** — `src/issue_agent/providers/<type>/`, mirroring the GitHub
   one:

   ```text
   providers/<type>/
   ├── plugin.py      → <Type>ProviderPlugin
   ├── settings.py    → <Type>ProviderSettings
   ├── webhook.py     → WebhookAdapter
   ├── client.py      → ForgeClient
   ├── pr.py          → PullRequestClient (if the forge has a PR API)
   └── transport.py   → GitTransport
   ```

2. **Settings union** — in
   [`providers/settings.py`](../src/issue_agent/providers/settings.py), add
   your model to `SETTINGS_BY_TYPE` and to the `Union[...]` in
   `ProviderSettings`.
3. **Plugin type** — in
   [`providers/registry.py`](../src/issue_agent/providers/registry.py), add
   your plugin to `BUILTIN_PROVIDER_TYPES`.
4. **Config docs** — document the new `type` and its fields in
   `config.yaml.example` and its secrets in `.env.example` and
   [operations.md § Configuration](./operations.md#configuration).
5. **Dependencies** — if you need an SDK, add it with `uv add`. A heavy or
   copyleft SDK is a candidate for an optional extra so GitHub-only deploys
   don't pull it (PRD §3.3); plain `httpx` calls are often enough.

Nothing else in the codebase should need to change.

## Testing

### The conformance suite

[`tests/unit/providers/test_provider_contract.py`](../tests/unit/providers/test_provider_contract.py)
runs the same checks against every provider: identity and capabilities,
valid/wrong/tampered/empty-secret signatures, stable and missing delivery
keys, each sample event parsed correctly and round-tripping through the
queue's JSON, malformed bodies rejected, credential-free clone URLs, scoped
`git_auth_env`, well-formed URLs and references, `pulls()` matching
`supports_pr_api`, and a `health_check()` that doesn't raise.

To enroll your plugin, add a **kit** to
[`tests/unit/providers/kits.py`](../tests/unit/providers/kits.py) and
register it in `KITS`. `test_every_builtin_type_has_a_kit` fails until you
do. A kit implements `ProviderKit`:

- `type` and `reports_association` (`False` if your webhook never says how
  an author relates to the repo).
- `make(instance_id, secret)` — build the plugin with its API credentials
  stubbed, so the outbound ports work offline (see `GitHubKit`, which hands
  the plugin a `MagicMock` auth).
- `delivery(sample, *, secret, delivery_key)` — a signed delivery, in your
  forge's real wire format, for each of the `SAMPLES`: `issue_opened`,
  `comment`, `comment_on_pr`, `own_bot_comment`,
  `labels_changed_without_trigger`, `unsupported`. When `delivery_key` is
  `None`, omit the key so the "missing key" case is exercised.

Use real payloads captured from the forge as JSON fixtures under
`tests/unit/providers/<type>/fixtures/`, trimmed of anything personal.

### Plugin unit tests

Put the rest under `tests/unit/providers/<type>/`, as the GitHub plugin does
(`test_webhook.py`, `test_signature.py`, `test_client.py`, `test_pr_ops.py`,
`test_transport.py`, `test_plugin.py`): settings validation, every event and
action you map, API calls with the HTTP layer mocked, a custom host, token
renewal. Tests that hit a real forge go in `tests/e2e/` and are opt-in.

### What you don't need to test

The core is already tested against `FakeProvider` in both variants — with
and without a PR API, with and without association. If your plugin passes
the conformance suite, the queue, worker and executor work with it.

## Security checklist

- [ ] Signature check uses a constant-time compare and fails closed on an
      empty secret.
- [ ] No rejection before signature verification sets `mark_reason`.
- [ ] Secrets are listed in `secret_fields` and never logged.
- [ ] `clone_url` never carries a credential; `git_auth_env` scopes it to the
      forge host and sets `GIT_TERMINAL_PROMPT=0`.
- [ ] Tokens are resolved inside the clients, per repo, and never returned to
      the core.
- [ ] The payload is treated as untrusted data: no `eval`, no templating of
      issue text, and size limits respected (`max_body_length`).
- [ ] `health_check()` makes no network call.

## Forge notes and prior research

The PRD's [roadmap](../PRD.md#9-future-roadmap-out-of-scope-for-this-prd) records what
is already known about GitLab (token vs. signing-token webhooks, `iid`,
nested groups, `oauth2:` git auth, `!N` references) and Gitea/Forgejo
(`X-Gitea-Signature`, `WIP:` drafts). Gogs, a forge without a PR API, has
its own spec in [PRD-gogs.md](../PRD-gogs.md).

## Opening the PR

Follow [CONTRIBUTING.md](../CONTRIBUTING.md): a `feat/<type>-provider`
branch from `origin/main`, Conventional Commits, `uv run ruff check` and
`uv run pytest` green, and the minor version bump in `pyproject.toml`
(`uv lock` after) as its own commit. It's fine to open a draft PR early with
just the settings model and the kit — the conformance suite is a good
progress bar.
