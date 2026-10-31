# Operations Runbook

## Configuration

Non-secret configuration lives in one commented file with all fields documented
inline — repos, budgets, worker tuning, LLM provider, PR/git identity, metrics
buckets, prompt routing, retention. Precedence is env var > config file > code
default.

- **Committed:** `config.yaml.example` — the template.
- **Real config:** `config.yaml` — **git-ignored**. Locally,
  `cp config.yaml.example config.yaml` and edit it.
- **In production:** the real `config.yaml` is **not** baked into the image. It
  ships as the `CONFIG_YAML` Fly secret (base64) and Fly writes it to
  `/app/config.yaml` at deploy time via the `[[files]]` block in `fly.toml`.
  Update it with:

  ```bash
  fly secrets set CONFIG_YAML="$(base64 -w0 < config.yaml)" --app <app-name>
  ```

  Changing the secret restarts the Machine automatically — no image redeploy
  needed. With no config file present at all, `Settings` falls back to env vars
  and code defaults (the agent will have no `repos:` and match no webhook).

**Provider instances:** each forge the service talks to is an instance under
`providers.<id>` in `config.yaml`, with a `type` (built-in: `github`) and the
type's non-secret fields (`app_id`, `bot_login`, `host`,
`token_refresh_margin_seconds` for GitHub). The id names the webhook URL
(`/webhooks/<id>`) and the env prefix of the instance's secrets
(`PROVIDERS__<ID>__<FIELD>`, `<ID>` in upper case). Env and YAML are
deep-merged per field, so the YAML holds the instance and the env adds its
secrets; any non-secret field can also be overridden from env (e.g.
`PROVIDERS__GITHUB__HOST`). A secret written in `config.yaml` is rejected at
startup. Several instances of one type can coexist (github.com plus a GitHub
Enterprise host); each `repos[]` entry then names its instance with
`provider:` (optional when only one instance is configured). The same
`owner/repo` may be routed on two instances; deliveries, queue entries and
per-issue work are kept apart per instance.

```yaml
providers:
  github:                      # keeps the /webhooks/github URL
    type: github
    app_id: 123
  ghe:
    type: github
    app_id: 7
    host: https://ghe.example.com
repos:
  - issues: owner/repo         # on github.com
    provider: github
  - issues: team/service       # on GHE
    provider: ghe
```

**Per-repo LLM override:** each entry in `repos:` may define its own `llm:`
block mirroring the global one (`repos[].llm` in `config.yaml.example`).
Resolution is field by field via `Settings.llm_profile(route)`: any field the
repo doesn't set falls back to the global `llm:` value, so precedence for a
single field is `repo llm override > env > config.yaml global > code default`.
An explicit `null` on `temperature`, `max_output_tokens`, `reasoning_effort`,
or the whole `fallback:` block resets that one repo back to "let the provider
decide" / "no failover", even when the global value is set — it is not the
same as leaving the key out. API keys (`api_key`, `fallback.api_key`) are
never accepted in a repo's `llm:` block — they always come from the global
`LLM_API_KEY` / `LLM_FALLBACK_API_KEY` secrets; config.yaml validation
rejects an attempt to set one there, and an unknown key under `repos[].llm`
(a typo) fails the same way instead of being silently ignored.

**`llm.reasoning_effort`** (`minimal`/`low`/`medium`/`high`/`xhigh`/`max`/`none`)
is passed straight to LiteLLM, which maps it to the provider's native
reasoning shape rather than sending it as-is. On an adaptive-thinking
Anthropic model (Opus 4.6+, Sonnet 4.6+) this becomes
`thinking={"type": "adaptive"}` plus `output_config.effort` — the current
control surface, since those models reject a fixed `budget_tokens`. On an
older model — including `anthropic/claude-sonnet-4-5`, the default in
`config.yaml.example` — there is no `output_config.effort`, so LiteLLM maps
each level to a **fixed thinking-token budget** instead
(`minimal`/`low` → 1024, `medium` → 2048, `high` → 4096, `xhigh` → 8192,
`max` → 16384). Two things follow from that: on an older model, that budget
must stay below `llm.max_output_tokens` or the provider returns a 400 that
mini-swe-agent retries repeatedly (`BadRequestError` is not one of its
abort-on-sight exceptions) before the task times out — leaving
`max_output_tokens` empty sidesteps this entirely. And a model with no
reasoning support at all aborts the task immediately
(`litellm.exceptions.UnsupportedParamsError`, which *is* an abort exception)
rather than silently ignoring the setting.

**`git.clone_depth`** controls how much history the workspace clone carries.
The default `0` clones the full history, because a run that ends in
`no_changes` has to say *when* the fix landed, not just that the code looks
right today — `prompts/system.md` asks for a `git log -S` lookup, and a
shallow clone has nothing to search. The agent cannot deepen the clone
itself: its sandbox gets an env allowlist with no GitHub token and
`GIT_TERMINAL_PROMPT=0` (`runner/environment.py`). Set a positive value to
bound clone time and volume space on a large repo, accepting that the
provenance lookup then finds nothing older than that boundary.

Secrets are set via environment variable / `fly secrets` (see `.env.example` at
the repo root):

| Variable | Required | Description |
|---|---|---|
| `CONFIG_YAML` | **Yes** (prod) | base64 of the real `config.yaml`, mounted at `/app/config.yaml` by `fly.toml` `[[files]]` |
| `PROVIDERS__<ID>__APP_PRIVATE_KEY` / `PROVIDERS__<ID>__APP_PRIVATE_KEY_PATH` | **Yes** (one of, per GitHub instance) | GitHub App private key, inline PEM or a filesystem path |
| `PROVIDERS__<ID>__WEBHOOK_SECRET` | **Yes** (per instance) | HMAC secret for webhook payload validation; empty rejects every delivery |
| `LLM_API_KEY` | **Yes** | API key for the LLM provider |
| `LLM_FALLBACK_API_KEY` | No | Key for `llm.fallback.model`; unset falls back to `LLM_API_KEY` |
| `ALERT_WEBHOOK_URL` | No | Webhook URL for budget-exceeded alerts (Discord, Telegram, etc.) |
| `FLY_API_TOKEN` | CI only | Not a `Settings` field — authenticates `flyctl` in the deploy workflow |

`<ID>` is the provider instance id in upper case (`github` → `GITHUB`). The
installation ID is **not** configured: the GitHub plugin resolves the
installation of each repo it talks to (issues and target repos separately)
with `GithubIntegration.get_repo_installation(owner, repo)`, cached per
process.

### Upgrading to 1.0.0 — provider instances

1.0.0 removes the GitHub-specific top-level settings in favour of
`providers.<id>` (PRD §8). It is a hard break: the old names are not read
any more, and a `config.yaml` that still has a `github:` or `github_app:`
section refuses to load. Mapping:

| Before (removed) | After |
|---|---|
| `github.app_id` / `GITHUB_APP_ID` | `providers.<id>.app_id` / `PROVIDERS__<ID>__APP_ID` |
| `GITHUB_APP_PRIVATE_KEY` / `GITHUB_APP_PRIVATE_KEY_PATH` (env only) | `PROVIDERS__<ID>__APP_PRIVATE_KEY` / `PROVIDERS__<ID>__APP_PRIVATE_KEY_PATH` (env only) |
| `GITHUB_WEBHOOK_SECRET` (env only) | `PROVIDERS__<ID>__WEBHOOK_SECRET` (env only) |
| `github.app_bot_login` / `GITHUB_APP_BOT_LOGIN` | `providers.<id>.bot_login` |
| `github.host` / `GITHUB_HOST` | `providers.<id>.host` (with the scheme) |
| `github_app.token_refresh_margin_seconds` | `providers.<id>.token_refresh_margin_seconds` |
| `agent.name` / `GITHUB_APP_NAME` | `agent.name` (unchanged) / `AGENT_NAME` |
| `git_identity.user_email` default `issue-agent[bot]@users.noreply.github.com` | default empty = the provider's no-reply address for `git_identity.user_name` (same value on GitHub) |
| `repos[].issues` / `repos[].target` | unchanged; new `repos[].provider` |

Keep the instance id `github` so the webhook URL configured on the GitHub App
(`/webhooks/github`) and the rows already in the database (assigned to
`github` by the schema migration) keep matching.

Deploy steps:

1. Wait for an empty queue (`SELECT status, COUNT(*) FROM tasks GROUP BY
   status` shows nothing `pending`/`running`) and take a volume snapshot
   (see *Snapshots*): the first start of 1.0.0 rebuilds
   `processed_deliveries` and `issue_work` (migration v7).
2. Rewrite `config.yaml` (`providers:` block; see `config.yaml.example`) and
   upload it, together with the renamed secrets:

   ```bash
   flyctl secrets set \
     CONFIG_YAML="$(base64 -w0 < config.yaml)" \
     PROVIDERS__GITHUB__APP_PRIVATE_KEY="$(cat path/to/private-key.pem)" \
     PROVIDERS__GITHUB__WEBHOOK_SECRET=<same value as before> \
     --stage --app <app-name>
   flyctl secrets unset GITHUB_APP_PRIVATE_KEY GITHUB_WEBHOOK_SECRET \
     GITHUB_APP_ID GITHUB_APP_BOT_LOGIN GITHUB_HOST GITHUB_APP_NAME \
     --stage --app <app-name>
   ```

   Unset only the names that exist (`flyctl secrets list`).
3. Release 1.0.0 (the deploy applies the staged secrets) and check
   `/readyz`: a missing secret shows up under `settings` with its new name,
   e.g. `missing: PROVIDERS__GITHUB__WEBHOOK_SECRET`.

Locally, rename the same variables in `.env` (see `.env.example`).

### Schema migrations

`SQLiteStore.init()` migrates the database in place on every start, and each
migration is idempotent. Migrations are **additive** (new tables, new
columns, new indexes) so an older image can still read the database after a
rollback. This is the rule earlier migrations cite as "P4". The one
exception is a primary key change, which SQLite can't do in place. Such a
migration rebuilds the table with a full copy in one transaction (v1–v3 on
`tasks`/`daily_budget`; v7 on `processed_deliveries` and `issue_work`, which
gained `provider_instance` in the key). Treat a release that contains one as
one-way: snapshot the volume first, and roll back by restoring the snapshot
rather than by redeploying the old image.

---

## The Agent Execution Contract

The coding agent (`prompts/system.md` + the per-category `bug_fix.md` /
`doc_fix.md`) runs under one hard invariant enforced by
`runner/executor.py`:

> **The agent edits files and leaves the changes in the working tree.
> The harness — not the agent — creates the branch, writes the commit,
> pushes, and opens the PR.**

Why it matters: the executor runs `git status --porcelain` after the agent
finishes. If the agent has **committed** its own edits, that comes back
clean and the run is treated as `no_changes` (aborted, work discarded).
There is a salvage path — the executor detects commits the agent made on
top of the clone point and folds them back into the tree (logged as
`agent_self_committed`) — but it is a safety net, not the design.

The agent's sandbox has **no push credential and no `gh`** (finding A2), so
it can never actually push or open a PR; the only failure mode is a local
commit.

A `no_changes` outcome is a verdict, not a failure, and the posted comment
says so. When the agent aborts because the requested change is *already* in
the repository, `prompts/system.md` requires its report to cite the commit
that introduced it (short SHA + subject, plus the PR number when the commit
subject carries one) and to recommend closing the issue with no change —
the agent cannot close an issue itself, so that recommendation is the only
signal a human gets. This is what `git.clone_depth: 0` exists to make
possible; see § Configuration.

A second deliberate `no_changes`: **a fix the sandbox cannot verify, resting
on a premise the agent did not check**. The agent
edits nothing and its submission names the unverified premise and carries the
proposed diff as plain text, so the issue gets a comment instead of a PR whose
regression test would enforce an unmeasured value. The comment header still
reads "no code change was needed"; the agent's report right below it is what
explains the stop.

**One exception to `no_changes`: a resumed branch that already carries the
work.** On a resume the executor checks out the existing work branch and
merges the base into it, so a clean tree there can mean "the previous run
already wrote this and never got a PR out of it" rather than "there is
nothing to do". Before aborting, the run therefore asks whether the branch
is ahead of `origin/<base>` *and* whether that difference has content — a
branch whose commits were squash-merged into the base is still ahead by the
merge commit alone and must not open an empty PR. If it does, nothing is
committed (there is nothing to stage) and the run goes straight to the
integrity check, push and PR, logged as `resumed_branch_already_ahead`.
Pushing there is also what makes the base merge outlive the workspace.

Because the agent's own report in that case reads "already fixed, close with
no change", the PR body and the issue comment open with a harness-written
note saying the branch already held the work, and the agent's report follows
it unedited. The PR named in that comment is always the run's own work
branch: `find_pr_by_branch` qualifies the GitHub `head` filter as
`owner:ref` — a bare ref is silently ignored by the API and returns every
open PR — and refuses any PR whose head is not the work branch rather than
reporting it (issue #89).

**Updating a resumed branch: the base merge and its conflicts.** Before the
agent runs on a resumed branch (a block retry, or a follow-up comment on an
issue whose PR is open), `_update_branch_with_base` merges `origin/<base>`
into it. The agent is told the outcome in the `<branch_status>` block of its
prompt (`runner/prompts.py` `build_branch_status`), with the PR number on a
follow-up:

- **merged** (`branch_update_merged`) — the branch is up to date; nothing is
  left to bring in.
- **conflicted** (`branch_update_conflict`, with `files`) — the merge is left
  *in progress*: markers in the listed files, every other path already
  merged. The agent resolves the markers by editing those files and nothing
  else — still no git. Afterwards `_finish_base_merge` stages only the
  conflicted paths and records a **two-parent merge commit** (`commit-tree`
  with the old tip and `origin/<base>` as parents, so it stays a merge even
  if the agent committed and dropped `MERGE_HEAD`; logged
  `base_merge_committed`). Any further edit the agent made becomes the usual
  harness commit on top. Because the base is then an ancestor of HEAD, the
  integrity check runs for real and catches an agent that ran
  `git merge --abort` and so dropped what the base gained.
- **failed** (`branch_update_failed`) — any other merge error; the merge is
  aborted and the branch left as it was.

If a conflicted file still has a `<<<<<<<` / `>>>>>>>` fence when the agent
finishes, the merge is abandoned and **nothing is pushed**: the task ends
`aborted_policy` with `error=merge_conflict_unresolved` (logged
`base_merge_unresolved`) and the issue gets a comment naming the files. The
work item goes back to `pr_opened` (or `branch_published`) on a follow-up,
so the next comment reopens the same branch; any other closed reason would
restart from the base and force-push over the delivered work. On a block (timeout, budget, crash) with
the merge still open, the harness records the merge if every marker is gone
and abandons it otherwise, *before* the WIP commit, which would otherwise
conclude the merge with its markers.

Why the merge is no longer aborted and hidden: told the branch was up to
date while it wasn't, and forbidden to run git, the agent answered "resolve
the conflicts" by copying the base's commits into files by hand — a
single-parent commit that leaves the PR's merge base where it was, so the PR
stayed conflicting, now in more files. The prompt now
forbids that hand-porting explicitly.

**Follow-ups report on the PR, not in its body.** The executor looks for an
already-open PR on the work branch (`PullRequestClient.find_open_pr`) and
reuses it untouched, so a follow-up never
rewrites the PR's title or body. Instead the run posts a comment on the PR
with the agent's summary, the base-merge outcome, the files changed and the
test result, and the issue comment says it *updated* the PR rather than
opened one. After any push from a resumed branch the harness also reads the
PR's mergeability for the pushed commit (`PullRequestClient.pr_has_conflicts`,
a few short polls while GitHub computes it). When the forge can't say (no
answer after the polls, or an API error), the harness fetches the base and
checks locally with `git merge-tree --write-tree origin/<base> HEAD`. If a
conflict is still reported, both comments carry a warning instead of
declaring success (logged `branch_still_conflicting`).

**Forges without a PR API publish the branch instead.** When the provider
has no `PullRequestClient` (`capabilities.supports_pr_api: false`, e.g. Gogs),
the run pushes the work branch exactly as before but opens no PR. The issue
gets one comment with a compare link (`transport.compare_url`), the summary,
the files changed and the test result. It never says the issue will close,
because nothing on the forge ties the branch to the issue. The labels a PR
would carry (`pr.label` plus the route's `labels`) go on the issue instead,
when the forge supports labels, so they must exist in the issue repo. The
work item closes as `branch_published` (no `pr_number`), and a follow-up
comment reopens it on the same branch, like `pr_opened`. Conflicts with the base are checked only
in the workspace (`git merge-tree`, after fetching the base again). Draft
settings don't apply, since there is no PR.

**Failure handling: the harness never discards a working tree.** The agent
loop can end in ways other than `Submitted` and the two limits. Every
non-success outcome except `no_changes` and the git/PR-stage failures goes
through the same salvage as a limit block (`_preserve_wip_on_block`): stage
everything, commit a `WIP: … ({error}) [skip ci]` commit, run the branch
integrity check, force-push the frozen work branch, store a handoff summary
on `issue_work`, and only then delete the workspace. The next trigger
resumes from that branch.

| Outcome (`error`) | Cause | Task status | `issue_work` | Retried |
|---|---|---|---|---|
| `llm_error` | The LLM provider/network failed (APIError, connection/SSL drop, timeout, 5xx, 429) after litellm's and mini-swe-agent's retry layers gave up (issue #246) | `failed_recoverable` | stays open (WIP on branch) | Automatically by `run_retry_recovery` with backoff, up to `budgets.max_task_retries`; a new comment also resumes at once |
| `provider_budget_exceeded` | The provider refused the call because a spending limit is exhausted (OpenRouter org/key budget guardrail, 402 insufficient credits). Detected on the first refusal and never retried; fires the alert webhook | `failed_terminal` | stays open (WIP on branch) | No auto-retry (nothing succeeds until the limit is raised or resets); a new comment resumes |
| `agent_crashed` | Any other exception out of the agent run or its construction (incl. non-retryable provider errors: auth, not-found, context window) | `failed_terminal` | stays open (WIP on branch) | No auto-retry (may be deterministic); a new comment resumes |
| `format_error` | 3 unparseable model replies in a row | `failed_terminal` | stays open (WIP on branch) | No auto-retry; a new comment resumes |
| `task_timeout`, `budget_exceeded`, `iteration_limit_exceeded`, `cost_limit_exceeded`, `cost_unavailable` | Limit blocks | `aborted_budget` | stays open (WIP on branch) | A new comment resumes |

Two cases still end the work item instead of keeping it open: nothing to
preserve (clean tree, branch not ahead of base → closed `no_changes`, task
`failed_terminal`) and the resume-attempt ceiling (`resume.max_attempts` →
closed `max_attempts`, branch kept for inspection). With `resume.enabled:
false` nothing is preserved and the work closes as `terminal_failure`. If
the WIP push itself fails, the comment says the work was lost and the next
run starts from scratch (`llm_error` is still re-queued).

The LLM request timeout (`llm.request_timeout_seconds`, default 120) makes a
dead connection fail fast into those retry layers instead of consuming
`budgets.task_timeout_seconds`.

### Prompt vocabulary per provider

No prompt names a forge. `prompts/*.md` and `INSTANCE_TEMPLATE`
(`runner/prompts.py`) render with the instance's `capabilities.terms`:
`{{ terms.issue }}`, `{{ terms.pr }}` ("Pull Request" on GitHub,
"Merge Request" on GitLab), `{{ terms.ref_prefix_pr }}`, and
`{{ instructions_files }}` (`AGENTS.md`, `CLAUDE.md`, plus the plugin's
`repo_instructions_files`, e.g. `.github/copilot-instructions.md`). The values
come from the plugin's static capabilities, never from a payload, so they
don't widen the template-injection surface. When editing a prompt, write
these placeholders instead of forge words: `tests/unit/test_prompts.py`
fails on "GitHub", "Pull Request", "PR" or a bare "issue".

### Containing the agent on a small VM

The Machine is small (1 shared vCPU, ~1 GB, no swap) and the worker, the web
server and everything the agent runs share it. Left alone, an agent that runs
`tsc` and `jest` side by side — and detaches them with `setsid … &` to escape
the per-command timeout — drives the VM into memory thrashing: `/readyz`
fails, SSH hangs, LLM sockets drop. The harness therefore contains it:

- **Nothing outlives its command.** Every command runs tagged with a
  per-environment marker (`ISSUE_AGENT_SANDBOX`, inherited by children even
  after `setsid`). When a command returns, every process still carrying the
  marker is killed (`runner/guards.py`), the observation says so, and the
  same sweep runs when the agent's run ends. A background job (`&`, `nohup`,
  `setsid`) therefore cannot survive; long commands must run in the
  foreground, one at a time. The harness' own `Reaped child process … SIGKILL`
  lines in `flyctl logs` are these kills and the `command_timeout_seconds`
  kills — not crashes.
- **Node's heap is capped** at `toolchains.nodejs.max_old_space_percent`
  (default 40) of the VM's total memory, read from `/proc/meminfo` at run
  time, so it follows the machine if it is resized (~385 MB of old space on
  the 1 GB VM). `toolchains.nodejs.max_old_space_mb` > 0 fixes an absolute
  value instead; `0` for both disables the cap. It is enforced by a `node`
  shim (`<toolchains.tools_root>/shims/node`) that always passes
  `--max-old-space-size` on the command line, which beats the agent's own
  `NODE_OPTIONS`; a `node` invoked by absolute path is not covered. Each
  task logs `node_heap_cap_resolved` with the value. Note V8's reported
  `heap_size_limit` is the cap plus a young-generation allowance (~192 MB on
  Node 24), so the process ceiling is higher than the cap itself. A repo too
  big for the cap fails fast with `JavaScript heap out of memory` in that one
  command instead of freezing the machine: raise the percentage (or resize the
  VM) rather than disabling it.
- **Lowest CPU priority.** Everything the harness runs for the agent
  (commands, dependency installs, the test command) goes through
  `nice -n 19`, so the worker and the web server stay responsive.
- **The prompts say so** (`RESOURCE LIMITS` in `prompts/system.md` and
  `system_diagnose.md`): one heavy command at a time, in the foreground, no
  detaching, no `NODE_OPTIONS`, narrow a command that timed out.

This reduces the risk; it does not replace memory. The only structural fix
for a project whose type-check or test run does not fit is a bigger Machine.

**If you customize the prompts, keep these rules:**

- Never instruct the agent to run `git add`, `git commit`, `git checkout` /
  `git switch`, `git branch`, `git reset`, `git push`, or `gh`.
- Target repos frequently ship a `CLAUDE.md` / `AGENTS.md` that *mandates*
  "create a branch, commit, run pre-commit". The system prompt must tell
  the agent to follow those files for **code style, structure, and how to
  run tests/lint**, but to **ignore** their git / branching / commit / PR
  steps.
- The agent finishes only via the `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
  signal; its final summary text becomes the commit message body and PR
  description.
- Never tell the agent the branch is up to date, or to "just make the
  changes" when asked to rebase. Point it at `<branch_status>` and keep the
  ban on copying the target branch's changes in by hand (see § Updating a
  resumed branch above).
- Read-only git (`git log`, `git log -S`, `git show`, `git blame`,
  `git diff`) must stay explicitly allowed. The forbidden list is a denylist
  of *mutating* verbs; read as a blanket ban, it suppresses the history
  lookup the "already fixed" report depends on.
- Keep the "already fixed" case in WHEN TO STOP: cite the commit, recommend
  closure, never guess a SHA, and end via the completion signal with a clean
  working tree (the agent has no `gh` and cannot comment on the issue
  itself — only the submission reaches GitHub, via the `no_changes`
  comment).
- Keep the `RESOURCE LIMITS` section (one heavy command at a time, in the
  foreground, no detaching, narrow a command that timed out) in both
  `system.md` and `system_diagnose.md`: the harness kills detached processes,
  so an agent that is not told wastes its steps relaunching them. It must
  stay ecosystem-agnostic (no `npm`/`Node.js` wording — see
  `test_no_hardcoded_ecosystem_stack_remains`).
- `tests/unit/test_prompts.py` locks these invariants — run it after any
  prompt edit.

---

## GitHub Integration

### GitHub App Configuration Reference

| Setting | Value |
|---|---|
| App name | From `agent.name` in `config.yaml` (shipped default: `issue-agent`); `AGENT_NAME` env overrides it. The bare code default if neither is set is `Issue Agent`. |
| Webhook URL | `<base-url>/webhooks/<instance id>` — `<base-url>/webhooks/github` for the `github` instance |
| Webhook secret | Set via `PROVIDERS__<ID>__WEBHOOK_SECRET` env |
| Repository permissions | Contents: Read & Write; Pull requests: Read & Write; Issues: Read & Write; Metadata: Read; Workflows: Read |
| Subscribe to events | Issues, Issue comment |

The agent handles `issues` and `issue_comment` events end to end.
Subscribing to more events than listed is harmless (they are dropped by the
event allowlist), but there is no need to.

`issue_comment` fires for comments on both issues and PR "Conversation" tab
threads (GitHub treats PRs as a superset of issues); the agent explicitly
ignores the latter (`reason: comment_on_pull_request`) so a comment on one of
its own PRs never enqueues another run — including when `repos[].target`
is the same repo as `repos[].issues`. See
[docs/trigger-conditions.md](./trigger-conditions.md) for the full trigger
pipeline. Reviewing/iterating on PR feedback is a distinct, not-yet-built
feature.

### Verify the App is Installed

```bash
# Resolve the installation ID for a repo and mint an installation token.
# GitHubAppAuth.resolve_installation_id(owner, repo) -> int
# GitHubAppAuth.get_token(installation_id) -> str
uv run python - <<'PY'
from issue_agent.config import get_settings
from issue_agent.providers.github.auth import GitHubAppAuth

auth = GitHubAppAuth(get_settings().providers["github"])
iid = auth.resolve_installation_id("OWNER", "REPO")
print("installation id:", iid)
print("token:", auth.get_token(iid)[:12] + "…")
PY
```

The app must be installed in both the **issue source repo** (where webhooks come from) and the **target repo** (where PRs are created). These may be the same repo. A missing installation makes `resolve_installation_id` raise a `404`.

### Check Webhook Deliveries (GitHub UI)

1. Go to the GitHub App settings page
2. Click **Advanced** (sidebar)
3. You will see the last 50 delivery attempts with status codes

Common status codes:
- `200 OK` — webhook accepted
- `202 Accepted` — webhook queued for processing
- `5xx` — agent unreachable; check Fly Machine status
- `4xx` — check webhook secret or payload

### Test the Webhook Manually

```bash
# Generate a test payload and sign it
PAYLOAD='{"action":"opened","issue":{"number":1,"title":"Test issue","body":"Test","labels":[]},"repository":{"full_name":"owner/test-repo"}}'
SIGNATURE=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$PROVIDERS__GITHUB__WEBHOOK_SECRET" | sed 's/^.* //')

curl -X POST https://<app-name>.fly.dev/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=$SIGNATURE" \
  -H "X-GitHub-Delivery: $(uuidgen)" \
  -H "X-GitHub-Event: issues" \
  -d "$PAYLOAD"
```

### Obtaining and Configuring the GitHub App Private Key

The GitHub App authenticates using a private key (PEM file) generated from the app settings page.

**Create the GitHub App (one-time):**

1. Go to `https://github.com/settings/apps` (org: `https://github.com/organizations/<org>/settings/apps`)
2. Click **New GitHub App**
3. Fill in the required fields:
   - **App name** — e.g. `my-issue-agent`
   - **Homepage URL** — your repo URL or app URL
   - **Webhook URL** — `https://<app-name>.fly.dev/webhooks/github`
   - **Webhook secret** — generate with `openssl rand -hex 32`
   - **Repository permissions** — Contents: Read & Write, Pull requests: Read & Write, Issues: Read & Write, Metadata: Read, Workflows: Read
   - **Subscribe to events** — Issues, Issue comment
4. Click **Create GitHub App**

**Download the private key:**

1. In the app settings page, scroll to the **Private keys** section
2. Click **Generate a private key**
3. A PEM file is downloaded automatically (e.g. `my-issue-agent.2026-01-01.private-key.pem`)
4. Store it securely — it cannot be downloaded again

**Set the key on Fly:**

```bash
# Option A: Set the PEM content directly as a secret
flyctl secrets set PROVIDERS__GITHUB__APP_PRIVATE_KEY="$(cat path/to/key.pem)" --app <app-name>

# Option B: Upload the PEM and reference by path (only works if the file is in the container)
# Not recommended for Fly — always use the secret for production
```

> **Important:** The PEM file contains newlines. The `$(cat ...)` syntax preserves them correctly. Do not inline the key as a single-line string.

**Install the app in repositories:**

1. In the app settings page, go to **Install App** (sidebar)
2. Choose the repositories where the agent will operate
3. The app must be installed in both the **issue source repo** and the **target repo**

The agent resolves the installation ID of each repo through the GitHub API
(cached per process), so there is nothing to configure here. To read it manually, see *Verify the App is Installed* above, or the
redirect URL right after installing: `https://github.com/settings/installations/<id>`.

---

## Onboarding an Issue Repository

For each `(issue repo, code repo)` pair the agent should act on:

1. Install the GitHub App on **both** repos (§ GitHub Integration).
2. Add the pair to `config.yaml` under `repos:` and re-push `CONFIG_YAML`
   (§ Configuration).
3. Create the routing labels — **the names are fixed in
   `policy/label_router.py`**; colours/descriptions are a suggestion (full
   contract in `docs/label-schema.md`).

**On the issue repo** — the 9 labels the router reads:

```bash
REPO=<owner>/<issue-repo>

gh label create bug               -R $REPO -c D73A4A -d "Bug — agent attempts a fix and opens a PR" --force
gh label create docs              -R $REPO -c 008672 -d "Docs/README/API — agent fixes and opens a PR" --force
gh label create chore             -R $REPO -c FBBD2F -d "Light refactor, lint, dependency bump — agent fixes and opens a PR" --force
gh label create question          -R $REPO -c D876E3 -d "Question — agent only comments (triage), a human decides" --force
gh label create feature-request   -R $REPO -c 0E8A16 -d "Feature request — agent only records it (triage)" --force
gh label create agent-fix-please  -R $REPO -c 1D76DB -d "Forces FIX on an issue with no other actionable label" --force
gh label create agent-skip        -R $REPO -c 000000 -d "Forces SKIP — wins over every other label" --force
gh label create severity:high     -R $REPO -c B60205 -d "High-severity bug — promoted to triage, no auto-fix" --force
gh label create severity:critical -R $REPO -c B60205 -d "Critical bug — promoted to triage, no auto-fix" --force
```

**On the code repo** — the PR label the agent applies to its own PRs
(`pr.label` in `config.yaml`, default `agent-generated`):

```bash
gh label create agent-generated -R <owner>/<code-repo> -c 5319E7 -d "PR opened automatically by the issue-agent" --force
```

`--force` makes it idempotent (creates or updates colour/description; `bug`,
`docs`, `question` often already exist as GitHub defaults).

Routing recap (default config): `bug` / `docs` / `chore` / `agent-fix-please` →
**FIX** (opens a PR); `question` / `feature-request` / `bug` +
`severity:high|critical` → **TRIAGE_ONLY** (comment only); `agent-skip` →
**SKIP**; an issue with none of these → **SKIP**. Which categories route to
TRIAGE_ONLY is configurable (`config.yaml`'s `policy.triage_only_categories`),
and there's a fourth action, **DIAGNOSE** (`policy.diagnose_categories`, empty
by default) — the agent inspects the repo read-only and posts a verdict on
whether the label matches reality, without editing files or opening a PR. See
`docs/label-schema.md` §1.1/§4.

4. Open one issue per category and confirm classification in the worker logs /
   `/metrics` before enabling real traffic.

---

## Deploy

### Standard Deploy

```bash
export FLY_APP=<app-name>     # or add --app <app-name> to every command
flyctl deploy --remote-only   # run from the repo root; fly.toml is there
```

The committed `fly.toml` has no `app` field, so `flyctl` takes the app from
`FLY_APP` (or `--app`); without either it stops with *"the config for your app
is missing an app name"* instead of targeting the wrong app.

This builds the Docker image remotely and deploys it. The Fly Machine restarts automatically with the new image.

### Deploy from CI

The GitHub Actions workflow (`.github/workflows/deploy.yml`) triggers when a
GitHub Release is **published** (tag `vX.Y.Z`, which must match the `version` in
`pyproject.toml`). It runs `flyctl deploy --remote-only --app "$FLY_APP"`
followed by `/healthz` and `/readyz` smoke tests against
`https://$FLY_APP.fly.dev`. Merges to `main` do not deploy on their own.

`FLY_APP` is a **repository variable** (not a secret — see § Fly API Token
for CI below for how to set it); without it the deploy step has no app to
target and the workflow fails.

```bash
# Cut a release (from an up-to-date main)
gh release create v0.1.0 --target main --generate-notes
```

Trigger the same workflow manually (redeploy the current `main`, e.g. for a
rollback) from the Actions tab via **workflow_dispatch**.

### Rollback

```bash
# List deployments to find the previous image
flyctl deployments list --app <app-name>

# Redeploy a specific version
flyctl deploy --app <app-name> --image <previous-image-tag>

# Or restore the volume from a snapshot if data corruption is involved
# (see "Volume and Data Management" > "Snapshots")
```

### Verify Deploy

```bash
flyctl status --app <app-name>
flyctl logs --app <app-name> --tail 20
curl https://<app-name>.fly.dev/healthz   # should return 200
curl https://<app-name>.fly.dev/readyz    # should return 200 + "status":"ready"
```

### Verifying the Deployed Version

Both `/healthz` and `/readyz` carry a `version` field — the installed
`fly-swe` distribution version, resolved once at startup. This is the
fastest way to confirm a given release actually reached production:

```bash
curl -s https://<app-name>.fly.dev/healthz | jq .version   # e.g. "0.2.1"
```

Compare it against the tag of the release you just published
(`gh release list`). If they don't match, `flyctl status --app <app-name>`
and the Actions run for that release's `Deploy` workflow are the next place
to look.

If the app isn't reachable at all, SSH in and check what's actually
installed in the container instead:

```bash
# Read the version straight from the file baked into the image
flyctl ssh console --app <app-name> --command "grep '^version' /app/pyproject.toml"

# Or from the installed package metadata (what /healthz itself reads) —
# use the venv's python, not the base image's system python
flyctl ssh console --app <app-name> --command \
  "/app/.venv/bin/python -c \"import importlib.metadata; print(importlib.metadata.version('fly-swe'))\""
```

### Environment Variables and Fly Secrets

Before deploying: `cp config.yaml.example config.yaml`, edit it for repos, model,
and budgets, then set it plus the true secrets as Fly secrets. Required secrets
are those marked **Yes** in the Configuration section above.

**Set all secrets at once** (run from the repo root, with a real `config.yaml`
in the working directory):

```bash
flyctl secrets set \
  CONFIG_YAML="$(base64 -w0 < config.yaml)" \
  PROVIDERS__GITHUB__APP_PRIVATE_KEY="$(cat path/to/private-key.pem)" \
  PROVIDERS__GITHUB__WEBHOOK_SECRET=<value> \
  LLM_API_KEY=<value> \
  --app <app-name>
```

Re-run `flyctl secrets set CONFIG_YAML="$(base64 -w0 < config.yaml)"` whenever
the config changes — the Machine restarts and picks it up without a redeploy.

> `llm.model` is a LiteLLM id (`<provider>/<model>`); the prefix, or an explicit
> `llm.provider`, selects the provider — the API key is never used to infer it.
> `LLM_FALLBACK_API_KEY` is an optional secret, only needed when `llm.fallback.model`
> is set (it defaults to `LLM_API_KEY`).

**Set or update a single secret:**

```bash
flyctl secrets set PROVIDERS__GITHUB__WEBHOOK_SECRET=<value> --app <app-name>
```

**Verify all secrets:**

```bash
flyctl secrets list --app <app-name>
```

> **Note:** Secrets are injected as environment variables at runtime. After changing secrets, the Machine restarts automatically. There is no need to redeploy the image for secret changes.

**Local development (.env):**

For local development, copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
# Edit .env with your values
```

The `.env` file is gitignored and must never be committed.

### Fly API Token for CI

The GitHub Actions deploy workflow (`.github/workflows/deploy.yml`) needs a Fly API token to authenticate `flyctl`.

**Obtain the token:**

```bash
flyctl auth token
```

This outputs a long-lived API token valid for the current authenticated account.

**Set it as a GitHub Actions secret:**

1. Go to your repository on GitHub: `Settings > Secrets and variables > Actions`
2. Click **New repository secret**
3. Name: `FLY_API_TOKEN`
4. Value: paste the token from `flyctl auth token`
5. Click **Add secret**

The CI workflow reads `${{ secrets.FLY_API_TOKEN }}` to authenticate `flyctl` during deployment.

> **Security note:** The Fly API token has full access to your Fly account. The
> deploy workflow only runs on published releases and manual `workflow_dispatch`,
> so the token is never exposed to pull-request builds.

### FLY_APP for CI

The deploy workflow also needs to know **which** Fly app to deploy to and
smoke-test. Unlike the token above, this isn't sensitive, so it's a
**variable**, not a secret — and the `deploy` job declares
`environment: default`, so it must be set as an **environment variable** on
that environment (a repository-level variable is not visible to a job that
declares an `environment:`, unless it's also added to that environment):

1. Go to your repository on GitHub: `Settings > Environments`
2. Open the `default` environment (create it first if it doesn't exist yet —
   any name works, as long as it matches the `environment:` value in
   `.github/workflows/deploy.yml`)
3. Under **Environment variables**, click **Add variable**
4. Name: `FLY_APP`
5. Value: the Fly app name — the one you passed to `flyctl apps create`
   (the committed `fly.toml` has no `app` field; locally the same name goes in
   `FLY_APP`, see *Standard Deploy*)
6. Click **Add variable**

The CI workflow reads `${{ vars.FLY_APP }}` to pass `--app` to `flyctl deploy`
and to build the smoke-test URLs. Set this before the first CI-triggered
deploy — a published release with `FLY_APP` unset fails at the deploy step
with no app to target.

---

## Monitoring

### Health Endpoints

| Endpoint | Expected Status | What it checks |
|---|---|---|
| `GET /healthz` | 200 | Liveness — always 200 while the process is up |
| `GET /readyz` | 200 / 503 | SQLite reachable, required settings present, each provider instance's own health check (GitHub: private key looks like a PEM), model function-calling support (advisory) |
| `GET /metrics` | 200 | Prometheus metrics in OpenMetrics format |

**`/healthz` response** (`service` is the slugified `agent.name`; `version` is
the installed `fly-swe` distribution version, resolved once at startup via
`importlib.metadata`):
```json
{"status": "healthy", "service": "issue-agent", "version": "0.2.1"}
```

**`/readyz` response (healthy):**
```json
{
  "status": "ready",
  "version": "0.2.1",
  "checks": {
    "sqlite": "ok",
    "settings": "ok",
    "github_auth": "ok",
    "github_own_bot_login": "ok",
    "llm_function_calling": "ok"
  }
}
```

**`/readyz` response (unhealthy):** status code 503
```json
{
  "status": "not_ready",
  "version": "0.2.1",
  "checks": {
    "sqlite": "ok",
    "settings": "missing: providers.github.app_id, REPO_ROUTES (config.yaml: repos), LLM_API_KEY",
    "github_auth": "private key not loaded",
    "github_own_bot_login": "ok",
    "llm_function_calling": "ok"
  }
}
```

Each provider instance reports its own health. Its missing configuration joins
`settings`; every other check is keyed `<instance_id>_<check>`, so the
`github` instance contributes `github_auth` and `github_own_bot_login`. Any
unhealthy instance makes the service not ready. `github_own_bot_login` is
informational: it warns when `providers.<id>.bot_login` is unset and never flips
readiness.

`llm_function_calling` is informational only — it reports `warning: ...` when
LiteLLM's capability map says `llm.model` may not support function/tool calling
(the agent drives the model through a bash tool and needs it). It never flips
readiness, because that map has false negatives for models LiteLLM doesn't know.
The same warning is logged once at startup.

### Metrics (`/metrics`)

Available metrics (Prometheus format):

```
issue_agent_tasks_total{status="succeeded|failed|aborted", provider_instance="<id>", repo="owner/name"}
issue_agent_task_duration_seconds           (histogram, no labels)
issue_agent_llm_cost_usd_total{provider_instance="<id>", repo="owner/name"}
issue_agent_iterations                      (histogram, no labels)
issue_agent_queue_depth
issue_agent_budget_used_today_usd{provider_instance="<id>", repo="owner/name"}
issue_agent_issue_work_resumes_total{provider_instance="<id>", repo="owner/name"}
issue_agent_issue_work_attempts{provider_instance="<id>", repo="owner/name"}   (histogram)
```

`provider_instance` is the `providers.<id>` the task came from: the same
`owner/name` can exist on two forges. Queries that only `sum by (repo)` keep
working; add `provider_instance` to tell the instances apart.

Access via `fly proxy` (since metrics port is internal):

```bash
fly proxy 9090:9090 --app <app-name>
# In another terminal:
curl http://localhost:9090/metrics
```

---

## Viewing Logs

### Basic Commands

```bash
# Tail live logs
flyctl logs --app <app-name>

# Tail with config file
flyctl logs --config path/to/fly.toml

# Show last N lines
flyctl logs --app <app-name> --tail 50
```

### Filtering

All logs are JSON structured. Use `jq` for filtering:

```bash
# Filter by log level
flyctl logs --app <app-name> | grep '"level":"error"'

# Filter by event name
flyctl logs --app <app-name> | grep '"event":"task_processed"'

# Filter by task_id
flyctl logs --app <app-name> | grep '"task_id":"<task-id>"'

# Extract structured fields
flyctl logs --app <app-name> | grep '"event":"worker_started"' | jq '.poll_interval, .orphan_timeout'
```

### Process-specific Logs

The agent has two processes: `web` and `worker`. Logs from both are interleaved. Each log line includes the process name:

```bash
# Worker logs only
flyctl logs --app <app-name> | grep '"logger":"issue_agent.worker"'

# Web server logs only
flyctl logs --app <app-name> | grep '"logger":"issue_agent.webhook"' 
```

### Audit Log

The audit log is a structured JSON file at `/data/audit.log` on the Fly volume. Access via SSH:

```bash
flyctl ssh console --app <app-name> --command "tail -50 /data/audit.log"
flyctl ssh console --app <app-name> --command "grep 'event.*task_processed' /data/audit.log | jq '.outcome, .cost_usd'"
```

Every entry about a task or an `issue_work` row (`task_processed`,
`orphan_recovery`, `issue_work_closed`, `issue_work_reconciled`,
`issue_work_dropped_on_reopen`) carries `provider_instance` next to the repo,
so `jq 'select(.provider_instance == "github")'` narrows the log to one forge.

---

## Queue Management

### Check Queue Depth

```bash
# Via metrics
curl http://localhost:9090/metrics | grep issue_agent_queue_depth

# Via SQLite directly
flyctl ssh console --app <app-name> --command \
  "sqlite3 /data/issue_agent.db 'SELECT status, COUNT(*) FROM tasks GROUP BY status;'"
```

### List Recent Tasks

```bash
flyctl ssh console --app <app-name> --command \
  "sqlite3 -header -column /data/issue_agent.db \
  'SELECT id, status, repo, issue_number, label_category, cost_usd, iterations, error, created_at FROM tasks ORDER BY created_at DESC LIMIT 20;'"
```

### Manually Retry a Failed Task

```bash
# Reset a failed/recoverable task back to pending
flyctl ssh console --app <app-name> --command \
  "sqlite3 /data/issue_agent.db \
  \"UPDATE tasks SET status='pending', retry_count=0, error=NULL, completed_at=NULL WHERE id='<task-id>' AND status='failed_recoverable';\""
```

### Clear Stuck Tasks

If a task is stuck as `running` and the orphan recovery isn't triggered:

```bash
flyctl ssh console --app <app-name> --command \
  "sqlite3 /data/issue_agent.db \
  \"UPDATE tasks SET status='failed_terminal', error='manually_cleared', completed_at=CURRENT_TIMESTAMP WHERE id='<task-id>' AND status='running';\""
```

---

## Pausing and Resuming the Agent

### Hard Pause (Scale to Zero)

Stops the entire Fly Machine. No webhooks or worker processing.

```bash
flyctl scale count 0 --app <app-name>
```

Resume:
```bash
flyctl scale count 1 --app <app-name>
```

There is no worker-only pause flag. To stop processing while still accepting
webhooks, drop the daily budget to `0` — new tasks queue but never run:

```bash
flyctl secrets set MAX_DAILY_BUDGET_USD=0 --app <app-name>
# resume: unset it (config.yaml value takes over again)
flyctl secrets unset MAX_DAILY_BUDGET_USD --app <app-name>
```

Queued tasks are picked up once the budget is restored (or at the next
daily reset).

### Per-Issue Skip via Label

Add the `agent-skip` label to a specific issue. The label router will skip it regardless of other labels. No deployment change needed.

---

## Budget Management

### Check Daily Spend

```bash
# Via metrics
curl http://localhost:9090/metrics | grep issue_agent_budget_used_today_usd

# Via SQLite
flyctl ssh console --app <app-name> --command \
  "sqlite3 -header /data/issue_agent.db 'SELECT * FROM daily_budget ORDER BY date DESC LIMIT 7;'"

# Via audit log
flyctl ssh console --app <app-name> --command \
  "grep daily_budget_exceeded /data/audit.log | tail -5 | jq '.spent_usd, .limit_usd'"
```

### When Budget is Exceeded

When `MAX_DAILY_BUDGET_USD` is exceeded:
1. The worker logs `daily_budget_exceeded` every poll cycle
2. An alert is sent to `ALERT_WEBHOOK_URL` (if configured)
3. New tasks stay in `pending` queue until the budget resets the next day
4. Tasks already running complete normally

### Increase or Reset Budget

Edit `budgets.max_daily_cost_usd` / `budgets.max_cost_per_task_usd` in your
`config.yaml` and re-push it (`flyctl secrets set CONFIG_YAML="$(base64 -w0 <
config.yaml)"`), or set `MAX_DAILY_BUDGET_USD` / `MAX_BUDGET_PER_TASK_USD` as a
Fly secret for a one-off override — env always wins over the config file.

```bash
# Manually reset today's budget (e.g., after testing)
flyctl ssh console --app <app-name> --command \
  "sqlite3 /data/issue_agent.db \"DELETE FROM daily_budget WHERE date=date('now');\""
```

---

## Volume and Data Management

### Disk Usage

```bash
# Check volume disk usage
flyctl ssh console --app <app-name> --command "df -h /data"

# List volume details
flyctl volumes list --app <app-name>

# Show event log sizes
flyctl ssh console --app <app-name> --command "du -sh /data/events/* | sort -rh | head -10"
```

### Snapshots

Snapshots are configured during initial provisioning:

```bash
# Check current snapshot retention
flyctl volumes list --app <app-name>

# Update retention (e.g., 7 days)
flyctl volumes update <volume-id> --snapshot-retention 7
```

To list them: `flyctl volumes snapshots list <volume-id>`. Fly takes one
block-level snapshot a day and keeps it for `--snapshot-retention` days
(default 5), so a restore loses everything written since the newest snapshot
(up to ~24 h). To restore, and to recreate the Machine, follow
[Recovering from a host failure](#recovering-from-a-host-failure-restore-a-snapshot-recreate-the-machine).

### Recovering from a host failure (restore a snapshot, recreate the Machine)

The app runs on **one Machine with one volume**, and a Fly volume lives on a
single host and is not replicated. If that host is down the whole service is
down: `flyctl deploy` cannot replace the Machine either (it stops with
*`machine … can't be automatically replaced because its volume … is on an
unreachable host`*). This section is the way out. Nothing here has been
exercised against the production app yet — see **Drill it before you need it**
at the end, and treat each Fly command as "check `flyctl <command> --help`
first".

| | Wait for the host | Restore a snapshot |
|---|---|---|
| Data lost | none | everything after the newest snapshot (≤ ~24 h) |
| Time | the host's outage (rolling reboots: ~15–20 min per host, per Fly) | minutes of work |
| When | Fly status shows a platform incident that is reboot-shaped | the host is not coming back, or the downtime costs more than the data |

Check [status.flyio.net](https://status.flyio.net/) first. When the host is
merely rebooting, waiting loses nothing and restoring throws away the freshest
data.

#### 1. Confirm it is the host

```bash
flyctl status --app <app-name>          # the Machine row carries a skull: host unreachable
flyctl machine list --app <app-name>    # "These Machines' hosts could not be reached"
flyctl volumes list --app <app-name>    # the volume is starred the same way
flyctl checks list --app <app-name>
curl -m 10 https://<app-name>.fly.dev/readyz
```

Note the **Machine id** and the **volume id**. A Machine that is unreachable
but `started` can come back on its own when the host does, with its old
database (see step 6).

#### 2. Pick the snapshot

```bash
flyctl volumes snapshots list <volume-id> --app <app-name>
```

Take the newest one with status `created`; its age is the data you will lose
(your RPO). The restored volume must be at least the snapshot's `VOL SIZE`
(3 GB for this app).

#### 3. Create the new Machine and volume from the snapshot

**Fast path** — clones the Machine's configuration (image, env, the
`CONFIG_YAML` file mount, VM size) and restores its volume from the snapshot in
one step:

```bash
flyctl machine clone <old-machine-id> --from-snapshot <snapshot-id|last> \
  --region <region> --app <app-name>
flyctl machine destroy <old-machine-id> --force --app <app-name>   # see step 6, do it now
```

The clone runs the **same image as the old Machine**, which may be older than
`main`; re-run the Deploy workflow afterwards (step 7).

**Explicit path** — when the clone cannot read the old Machine's config, or you
want a clean deploy:

```bash
flyctl volumes create issue_agent_data --snapshot-id <snapshot-id> \
  --region <region> --size 3 --app <app-name>
flyctl machine destroy <old-machine-id> --force --app <app-name>
flyctl deploy --remote-only --app <app-name>      # or run the Deploy workflow
```

`flyctl deploy` creates the Machine from `fly.toml` and mounts a free volume
named `issue_agent_data` in its region. Secrets (`CONFIG_YAML`, the GitHub App
key, …) belong to the app, not the Machine, so they are still there.

Region: `--region` is accepted by both `volumes create` and `machine clone`;
keep it equal to `primary_region` in `fly.toml` (gru) or pass the target region
to the deploy. Whether a snapshot can be restored into a **different region**
is not stated in `flyctl … --help` or the Fly docs consulted — try it, and fall
back to the same region (the volume is placed in a different hardware zone than
the existing one by default, which is usually a different host).

#### 4. Verify the restored service

```bash
flyctl status --app <app-name>
flyctl logs --app <app-name>
curl -s https://<app-name>.fly.dev/readyz     # the llm_function_calling warning is informational
flyctl ssh console --app <app-name> --command "python3 -c \"import sqlite3; c = sqlite3.connect('/data/issue_agent.db'); print(c.execute('PRAGMA integrity_check').fetchone(), c.execute('SELECT status, count(*) FROM tasks GROUP BY status').fetchall(), c.execute('SELECT max(created_at) FROM tasks').fetchone())\""
```

`integrity_check` must say `ok`, and the newest `tasks.created_at` should be
about as old as the snapshot.

#### 5. Reconcile what was written after the snapshot

The restored database does not know about anything that happened after the
snapshot. Work through these, newest concern first:

- **Work in flight.** `issue_work` rows and tasks created after the snapshot
  are gone, but the agent's output is on GitHub: compare the open
  `agent-generated` PRs and the agent's WIP branches in the target repo with
  `issue_work`. An issue whose work the database no longer knows can be handed
  back by re-adding its label or commenting on it (any comment from an allowed
  author on an open issue enqueues a task — see
  [trigger-conditions.md](./trigger-conditions.md)); a WIP branch is resumed
  from the remote.
- **Webhooks.** GitHub does not redeliver failed deliveries by itself. In the
  App's settings → *Advanced* → *Recent deliveries*, redeliver the ones that
  **failed during the outage**. Do **not** redeliver ones that succeeded after
  the snapshot: `processed_deliveries` was restored from before them, so they
  would be processed twice (duplicate tasks, comments or PRs). GitHub's docs
  do not say how long deliveries are kept — do this promptly.
- **Budget.** `daily_budget` lost the spend after the snapshot, so
  `budgets.max_daily_cost_usd` undercounts today. Check the real spend at the
  LLM provider and, if needed, lower the limit for the day (see *Budget
  Management*).
- **History.** Audit logs and trajectories written after the snapshot stay on
  the old volume.

#### 6. The old Machine and the old volume

- **Do not let two Machines serve at once.** Two Machines mean two
  independent databases (see
  [plans/litefs-ha.md](./plans/litefs-ha.md) § *Why two plain Machines do not
  work*): duplicate tasks, doubled budget, two agents on one issue. Destroy
  the old Machine **as soon as the new one exists** (the `destroy --force`
  above). If the host returns first and the old Machine starts, stop it
  (`flyctl machine stop <old-machine-id>`) before it takes webhooks. Whether
  the control plane accepts a destroy while the host is unreachable is not
  verified; if it refuses, retry once the host is back.
- **The old volume has the newest data** — up to a day more than the snapshot.
  Once its host is back it is a free volume still named `issue_agent_data`,
  which a later `flyctl deploy` or scale could attach to a new Machine. Before
  any deploy, fork it under another name, then inspect it:

  ```bash
  flyctl volumes list --app <app-name>
  flyctl volumes fork <old-volume-id> --name issue_agent_old --app <app-name>
  ```

  Read what you need from the fork (for example the `tasks`/`issue_work` rows
  after the snapshot) by mounting it on a short-lived Machine, then
  `flyctl volumes destroy` the original and, when you are done, the fork.
  Destroying a volume deletes its data permanently.

#### 7. After the recovery

- Make sure exactly **one** Machine is running (`flyctl machine list`).
- Re-run the **Deploy** workflow (or `flyctl deploy`) if the version running is
  not `main`'s (see *Verifying the Deployed Version*).
- Remember `FLY_APP` (the committed `fly.toml` has no app name).
- Write down the real RPO and RTO of this recovery.

#### Drill it before you need it

Nothing above has been run against production. In a scratch Fly app, create a
volume, put a SQLite file on it, take a snapshot (`flyctl volumes snapshots
create`), then do steps 3–4 with `machine clone --from-snapshot` and with
`volumes create --snapshot-id` + `flyctl deploy`, and time it. Also try the
region question and `machine destroy --force` on a Machine whose host you
cannot reach. Update this section with what actually happens. A daily snapshot
is also the **only** protection the data has today; if a day of lost state or an
hour of downtime is too much, see
[plans/litefs-ha.md](./plans/litefs-ha.md).

### Clean Up Old Data

The worker runs `run_maintenance()` on startup and then every
`retention.cleanup_interval_minutes` (default 60). It prunes, per the
`retention:` block in `config.yaml`:

| What | Governed by | Default |
|---|---|---|
| `/data/events/<task-id>/` trajectory dirs (for completed tasks) | `retention.events_days` | 30 |
| `tasks` / `task_events` / `processed_deliveries` / `daily_budget` rows | `retention.db_days` | 90 |
| `/data/audit.<date>.log` files (gzipped on expiry) | `retention.audit_days` | 90 |
| `/data/workspaces/<task-id>/` dirs not tied to a running task | (always) | — |

**Not pruned:** `issue_work` rows (per-issue work state — kept while the
issue is open, independent of task retention) and the `agent/issue-<n>-*`
**WIP branches** the agent force-pushes to the target repo when a run blocks
on a timeout/budget stop (`resume.push_wip_on_block`). A WIP branch has no PR
until a later attempt succeeds; one left behind after `resume.max_attempts`
(the abandonment comment names it) is safe to delete by hand once a human has
looked at it. There is no automatic branch cleanup by design — deleting the
agent's work in a customer repo needs its own policy.

Manual cleanup, if ever needed:

```bash
# Trajectory dirs older than the events window
flyctl ssh console --app <app-name> --command \
  "find /data/events -mindepth 1 -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +"

# DB rows (task_events references tasks(id), so delete events first)
flyctl ssh console --app <app-name> --command \
  "sqlite3 /data/issue_agent.db \"DELETE FROM task_events WHERE created_at < date('now','-90 days'); DELETE FROM tasks WHERE created_at < date('now','-90 days');\""
```

---

## Orphaned Task Recovery

### How Automatic Recovery Works

When a Fly Machine crashes or restarts during task execution:
1. The worker updates `heartbeat_at` every 30s while running a task
2. On startup, the worker queries for tasks with `status='running'` AND `heartbeat_at < now() - 90s`
3. Each orphan is either:
   - **Retried** (if `retry_count < 3`): workspace cleaned, status reset to `pending`
   - **Escalated** (if `retry_count >= 3`): marked `failed_terminal`, issue commented

This runs **before** the main polling loop, so state is consistent within seconds of restart.

### Monitor Recovery

```bash
# Check for orphan recovery events in logs
flyctl logs --app <app-name> | grep '"event":"orphan_recovery_complete"'

# Check for orphan recovery in audit log
flyctl ssh console --app <app-name> --command \
  "grep 'orphan_recovery' /data/audit.log | jq '.task_id, .action, .retry_count'"
```

### Manual Intervention

If a task is stuck as `running` and the worker is healthy:

```bash
# Force it to pending for retry
flyctl ssh console --app <app-name> --command \
  "sqlite3 /data/issue_agent.db \
  \"UPDATE tasks SET status='pending', retry_count=0, heartbeat_at=NULL WHERE id='<task-id>' AND status='running';\""

# Clean up orphaned workspace
flyctl ssh console --app <app-name> --command \
  "rm -rf /data/workspaces/<task-id>"
```

---

## Backup

### Audit Log Backup

Audit entries are written to a per-day file `/data/audit.<YYYY-MM-DD>.log`,
with `/data/audit.log` symlinked to today's. `run_maintenance()` gzips and
then removes files older than `retention.audit_days` (default 90).

```bash
# Check which audit log files exist
flyctl ssh console --app <app-name> --command "ls -la /data/audit*"

# Manual backup of audit log
flyctl ssh console --app <app-name> --command \
  "gzip -c /data/audit.log > /tmp/audit.$(date +%Y-%m-%d).log.gz"
# Download to local machine
flyctl ssh console --app <app-name> --command "cat /tmp/audit.$(date +%Y-%m-%d).log.gz" > ./audit-backup.log.gz
```

### Database Backup

```bash
# Backup SQLite while running (WAL mode allows concurrent reads)
flyctl ssh console --app <app-name> --command \
  "sqlite3 /data/issue_agent.db '.backup /tmp/issue_agent_backup.db'"

# Download
flyctl ssh console --app <app-name> --command "cat /tmp/issue_agent_backup.db" > ./issue_agent_backup.db
```

---

## Credential Rotation

### Webhook Secret (Every 90 Days)

```bash
# 1. Generate new secret
NEW_SECRET=$(openssl rand -hex 32)

# 2. Update on Fly
flyctl secrets set PROVIDERS__GITHUB__WEBHOOK_SECRET=$NEW_SECRET --app <app-name>

# 3. Update in GitHub App settings (Settings > Webhook > Secret)
#    The GitHub App config page is at:
#    https://github.com/settings/apps/<app-name>

# 4. Verify: check logs for successful webhook processing
```

### GitHub App Private Key (Every 180 Days)

GitHub App supports multiple active keys for gradual rotation.

```bash
# 1. Generate new key in GitHub App settings (Generate private key)
#    Download saves as: <app-name>.<date>.private-key.pem

# 2. Update on Fly
flyctl secrets set PROVIDERS__GITHUB__APP_PRIVATE_KEY="$(cat path/to/new-key.pem)" --app <app-name>

# 3. Verify on next Machine restart

# 4. After confirming the new key works, remove the old key from GitHub App settings
```

### LLM API Key (Every 90 Days)

```bash
# 1. Generate new key at the LLM provider console

# 2. Update on Fly
flyctl secrets set LLM_API_KEY=<new-key> --app <app-name>

# 3. Update local .env if applicable

# 4. Verify: check logs and open a test issue
```

### Verify All Secrets

```bash
flyctl secrets list --app <app-name>
```
