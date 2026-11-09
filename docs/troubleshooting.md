# Troubleshooting

## Webhook Received but No Task Created

**Symptoms:**
- Issue opened/labeled but no task appears in the queue
- GitHub webhook delivery log shows `200` or `202` but no visible effect

**Probable causes and checks:**

1. **Delivery ID already processed** — check `processed_deliveries` table:
   ```bash
   flyctl ssh console --app <app-name> --command \
     "sqlite3 /data/issue_agent.db \"SELECT * FROM processed_deliveries ORDER BY received_at DESC LIMIT 5;\""
   ```

2. **Event type not in allowlist** — the agent only processes specific events (issues opened/labeled, issue comments with commands, etc.). Check logs:
   ```bash
   flyctl logs --app <app-name> | grep "unsupported_event"
   ```

3. **Label routing resulted in `skip`** — if the issue has no actionable label, the agent ignores it. Check if labels like `bug`, `docs`, `chore`, or `agent-fix-please` are present.

4. **Webhook payload parsing failed** — check logs for JSON errors:
   ```bash
   flyctl logs --app <app-name> | grep "parse_error\|invalid_payload"
   ```

5. **HMAC validation failed** — check for signature errors:
   ```bash
   flyctl logs --app <app-name> | grep "invalid_signature\|hmac"
   ```

**Resolution:**
- If the delivery was already processed, it's working as designed (idempotency)
- Add an actionable label to the issue
- Verify webhook secret matches between GitHub App and `flyctl secrets list`

---

## `POST /webhooks/github` Returning 400

Uvicorn's access line (`"POST /webhooks/github HTTP/1.1" 400`) does not say why;
the application log line right next to it does. `flyctl logs --app <app-name> | grep -E
"missing_delivery_header|invalid_signature|invalid_json|invalid_payload"`:

| Log event | Meaning | Fix |
|---|---|---|
| `missing_delivery_header` | No `X-GitHub-Delivery` header — not a GitHub request (probe, proxy stripping headers) | Nothing to do for stray traffic; otherwise check the proxy in front of the app |
| `invalid_signature` | HMAC doesn't match the instance's `PROVIDERS__<ID>__WEBHOOK_SECRET`, or the secret is empty — an empty secret rejects every delivery (fail-closed, since v0.11.0) | Make the secret on the GitHub App (and any repo/org webhook pointing at this URL) match `flyctl secrets list`; set it if it's empty |
| `invalid_json` | Signed body isn't JSON — usually the webhook's content type is `application/x-www-form-urlencoded` | Set the webhook content type to `application/json` |
| `invalid_payload` | JSON that can't be normalized into a `TriggerEvent` (e.g. a non-integer `issue.number`) — not something GitHub sends | Inspect the delivery in the App's *Advanced* tab; a proxy rewriting the body is the usual suspect |

A `404` with `{"error": "unknown provider instance"}` means the path names no
configured provider instance: the GitHub App must post to `/webhooks/github`
(log event `unknown_provider_instance`).

Signed events other than `issues` / `issue_comment` (`pull_request`, `push`,
`check_suite`, …) are **not** errors: they get a `200` with
`{"status": "ignored", "reason": "unsupported_event"}` and an `unsupported_event`
log line, e.g. after a PR merge when the App subscribes to more events than it
needs. Before this behaviour they returned `400`, which GitHub records as failed
deliveries.

---


## Task Failed with `invalid_trigger`

The worker reads each task's canonical trigger (`tasks.trigger_json`); the
raw webhook payload is not stored. The payload guard rejects a task whose
`trigger_json` is missing or invalid with `failed_terminal` and
`error = invalid_trigger`, and posts the usual "task not processed" comment.

A missing trigger only happens on a database upgraded straight from a
release older than v0.10. Those releases enqueued tasks with only the raw
payload, and the startup backfill that re-normalized them was removed in
v1.1.10. Upgrade through any release from v0.10.0 to v1.1.9 first to keep such
in-flight tasks.

Neither is expected for deliveries GitHub actually sent. To retry the
issue, comment on it or re-apply its label: that creates a new task.

---

## Tasks Stuck Pending (Worker Not Dequeuing)

**Symptoms:**
- Tasks remain in `pending` status indefinitely
- Worker appears to be running (`flyctl logs` shows worker loop heartbeats)

**Probable causes and checks:**

1. **Daily budget exceeded** — the most common cause. Check:
   ```bash
   curl http://localhost:9090/metrics | grep issue_agent_budget_used_today_usd
   ```
   Look for `daily_budget_exceeded` in logs:
   ```bash
   flyctl logs --app <app-name> | grep daily_budget_exceeded
   ```

2. **Worker in budget-cooldown loop** — when budget is exceeded, the worker polls but skips dequeuing. Logs show `daily_budget_exceeded` every 2 seconds.

3. **SQLite locked** — check if another process holds a write lock:
   ```bash
   flyctl logs --app <app-name> | grep "database_is_locked\|locked"
   ```

**Resolution:**
- Wait for the daily budget to reset at midnight UTC, or manually clear it (see Operations Runbook → *Budget Management* → *Increase or Reset Budget*)
- If locked, restart the Machine: `flyctl apps restart <app-name>`

---

## Task Stuck Running Indefinitely (Orphaned)

**Symptoms:**
- Task has `status='running'` for minutes without progress
- Worker logs show no heartbeat updates from this task

**Probable causes:**
- Machine crashed during execution (OOM, deploy, hardware failure)
- Worker process was killed mid-task
- Agent entered an infinite loop that bypassed `step_limit`

**Diagnosis:**
```bash
# Check for orphaned tasks
flyctl ssh console --app <app-name> --command \
  "sqlite3 /data/issue_agent.db \"SELECT id, heartbeat_at, retry_count FROM tasks WHERE status='running' AND heartbeat_at < datetime('now', '-90 seconds');\""
```

**Resolution:**
1. **Automatic** — restarting the Machine triggers orphan recovery. Because the worker checks for orphans on startup, state is usually consistent within seconds of a restart.

2. **Manual** — if the automatic recovery didn't fire (e.g., worker is running but missed this task):
   ```bash
   flyctl ssh console --app <app-name> --command \
     "sqlite3 /data/issue_agent.db \"UPDATE tasks SET status='pending', retry_count=0, heartbeat_at=NULL WHERE id='<task-id>' AND status='running';\""
   flyctl ssh console --app <app-name> --command "rm -rf /data/workspaces/<task-id>"
   ```

3. **Force terminal** — if the task has failed 3 times already:
   ```bash
   flyctl ssh console --app <app-name> --command \
     "sqlite3 /data/issue_agent.db \"UPDATE tasks SET status='failed_terminal', error='orphan_manual_clear', completed_at=CURRENT_TIMESTAMP WHERE id='<task-id>';\""
   ```

---

## Machine Host Unreachable (Deploy Fails on the Volume)

**Symptoms:**
- `flyctl status` shows the Machine with a skull and *"The machine's host is
  unreachable"*; `flyctl machine list` / `volumes list` star it with *"hosts
  could not be reached"*
- `/readyz` and SSH time out, and the app is down
- `flyctl deploy` (and the Deploy workflow) builds and pushes the image, then
  fails with `machine … requires manual intervention, it can't be automatically
  replaced because its volume … is on an unreachable host`

**Probable cause:** the physical host of the Machine and its volume is down
(a platform incident such as rolling host reboots, or a hardware failure). The
app has one Machine and one volume, so there is nothing to fail over to.

**Diagnosis:** check [status.flyio.net](https://status.flyio.net/) for an
incident covering your region, then `flyctl machine list --app <app-name>`.
This is not memory thrashing (see the entry below): there, the Machine answers
SSH and `dmesg` shows OOM kills.

**Resolution:** if the incident is reboot-shaped, **wait** — the host returns
with its data intact. Otherwise restore the newest snapshot and recreate the
Machine: runbook in [operations.md § Recovering from a host
failure](./operations.md#recovering-from-a-host-failure-restore-a-snapshot-recreate-the-machine).
It loses everything written after the newest daily snapshot.

---

## Machine Unresponsive During a Task (Memory Thrashing)

**Symptoms:**
- The Fly health check flips to `critical` (`Client.Timeout exceeded while
  awaiting headers`), `/readyz` times out, `flyctl ssh console` prints nothing
- A task shows many LLM retries (`SSL: UNEXPECTED_EOF_WHILE_READING`,
  `Server disconnected without sending a response`) or ends in
  `task_timeout` far after its limit
- `Reaped child process … SIGKILL` lines in `flyctl logs` (these are the
  harness killing a timed-out or detached command — expected, see
  *Containing the agent on a small VM* in the runbook)

**Diagnosis** (needs SSH to answer; if it does not, check
[status.flyio.net](https://status.flyio.net/) for a platform incident first):
```bash
flyctl ssh console --app <app-name> --command \
  "sh -c 'cat /proc/loadavg; grep -E \"MemTotal|MemAvailable|SwapTotal\" /proc/meminfo; cat /proc/pressure/memory; dmesg | grep -E \"Out of memory|oom-kill\" | tail'"
```
`MemAvailable` near 0 with `SwapTotal: 0`, a `full` memory pressure well above
10%, and a CPU that is mostly idle while the machine crawls (iowait) is
thrashing: the page cache and binaries are evicted and re-read from disk.
`Out of memory: Killed process … (node)` confirms the kernel had to pick a
victim. Look at what is running (`ls /proc/<pid>/cmdline` for `node`, `tsc`,
`jest`) and which task owns it (`/data/workspaces/<task-id>`).

**Resolution:**
1. The harness already kills detached processes after each command and caps
   Node's heap (`toolchains.nodejs.max_old_space_percent`). Check the task's
   `node_heap_cap_resolved` log line to see the value in effect.
2. If a repo's type-check/test run still does not fit, a bigger Machine is the
   structural fix (`[[vm]] memory_mb` in `fly.toml`); the cap scales with it
   by itself. Lowering the percentage only trades thrashing for
   `JavaScript heap out of memory` in that command.
3. The agent is told to run one heavy command at a time; a prompt customised
   without the `RESOURCE LIMITS` section loses that.

---

## `/readyz` Returning 503

**Symptoms:**
- `curl https://<app>.fly.dev/readyz` returns HTTP 503
- Fly proxy may not route external traffic to the Machine
- The app may appear unresponsive from outside

**Diagnosis:**
Check the response body — it explains which check failed:
```bash
curl -s https://<app>.fly.dev/readyz | jq .
```

Example unhealthy response:
```json
{
  "status": "not_ready",
  "checks": {
    "sqlite": "ok",
    "settings": "missing: providers.github.app_id, REPO_ROUTES (config.yaml: repos), LLM_API_KEY",
    "github_auth": "private key not loaded",
    "llm_model": "ok"
  }
}
```

`settings` covers each provider instance's config (`providers.<id>.app_id`,
`PROVIDERS__<ID>__WEBHOOK_SECRET`), `LLM_API_KEY` and `REPO_ROUTES` (the
`repos:` list in `config.yaml`). The other
GitHub checks come from the `github` provider instance and are prefixed with
its id (`github_auth`, `github_own_bot_login`). `llm_model` and
`github_own_bot_login` are informational and never fail readiness.

**Common failures and resolutions:**

| Check failure | Cause | Fix |
|---|---|---|
| `settings` missing secrets | Required secret env vars not set | `flyctl secrets set` the missing vars |
| `settings` missing `REPO_ROUTES` | `config.yaml` has no `repos:` entries | Add at least one `issues:` pair under `repos:` |
| `github_auth` — private key not loaded | `PROVIDERS__GITHUB__APP_PRIVATE_KEY` empty or `PROVIDERS__GITHUB__APP_PRIVATE_KEY_PATH` pointing to a non-existent file | Set the key directly via `flyctl secrets set PROVIDERS__GITHUB__APP_PRIVATE_KEY="$(cat key.pem)"` |
| Startup fails: `config.yaml sections github ... were replaced by providers.<id>` | A pre-1.0.0 `config.yaml` | Rewrite it with a `providers:` block — see *Upgrading to 1.0.0* in [operations.md](./operations.md) |
| Everything still configured with `GITHUB_*` secrets after an upgrade | 1.0.0 no longer reads the old names | Re-set them as `PROVIDERS__GITHUB__*` and unset the old ones (same guide) |
| `sqlite` failure | DB file missing or corrupt | Restore from snapshot or re-create (data loss) |
| `github_auth` — does not look like a valid PEM | Key does not start with `-----BEGIN` | Re-copy the PEM whole; both `RSA PRIVATE KEY` (PKCS#1) and `PRIVATE KEY` (PKCS#8) headers are accepted |

---

## LLM Errors / Rate Limits

**Symptoms:**
- Task fails with `failed_terminal`
- Logs show HTTP 429, 5xx, or timeout errors from the LLM provider

**Diagnosis:**
```bash
# Check for LLM-related errors
flyctl logs --app <app-name> | grep "llm_error\|rate_limit\|429\|5xx\|timeout"
```

**Probable causes:**

1. **Rate limited (429)** — the provider is throttling requests. The agent retries automatically up to `LLM_MAX_RETRIES` (default: 5).

   **Provider/network failure mid-run** (`litellm.APIError`, `[SSL: UNEXPECTED_EOF_WHILE_READING]`,
   connection reset, timeout, 5xx) — the task ends as `llm_error`
   *after* the retries (`llm.max_retries` + 1 attempts, exponential backoff) are
   exhausted. The work is not lost: the harness commits the working tree to the
   issue's work branch (`WIP: … (llm_error)`), the comment names the branch,
   and the task is re-queued automatically with backoff (up to
   `budgets.max_task_retries`). Commenting on the issue resumes it right away.
   Tune `llm.request_timeout_seconds` (default 120) if hung calls are the
   problem.

   **Hung call (no response, no error)** — the provider accepts the request and
   never answers. Each attempt waits out `llm.request_timeout_seconds`, but is
   capped at the time left of `budgets.task_timeout_seconds`, so the task ends as
   `task_timeout` at the budget instead of running far past it. On OpenRouter,
   retries also send a fresh `session_id` so the request is not routed back to
   the same (stalled) provider — OpenRouter's sticky routing otherwise pins a
   conversation to one provider while its prompt cache is warm. To spot it in a
   finished task, look for a very long gap before one assistant message in
   `/data/events/<task-id>/trajectory.json` (the `extra.timestamp` of an
   assistant message minus the previous message's).

   **Provider spending limit exhausted** (`provider_budget_exceeded`) — e.g.
   `Budget limit exceeded (monthly limit). … $10.02 of $10.00 used; resets
   2026-11-01…` from an OpenRouter budget guardrail (HTTP 403), or `Insufficient
   credits` (402). LiteLLM surfaces the guardrail as a generic `APIError`; the
   harness recognises it, aborts on the first refusal instead of retrying, keeps
   the WIP on the work branch, sends the alert webhook
   (`provider_budget_exceeded`, with spent/limit) and comments the provider's
   message and reset date on the issue. The task is **not** re-queued. Raise
   the limit in the OpenRouter dashboard (or wait for the reset), then comment
   on the issue to resume. Logs: `agent_run_provider_budget_exceeded`.

2. **Authentication (401)** — API key is invalid, expired, or **paired with a
   model prefix that does not route to the key's provider**. The model is
   `llm.model` in `config.yaml` (env `LLM_MODEL` overrides it); the key is the
   `LLM_API_KEY` secret. LiteLLM routes purely by the model prefix.
   - `DeepseekException - {"error":{"message":"User not found.","code":401}}`
     (or the same for another provider) means LiteLLM called that provider
     **directly** while `LLM_API_KEY` is an OpenRouter key (`sk-or-v1-...`).
     Fix: set the model back to an `openrouter/...` (or `@preset/...`) value in
     `config.yaml` and redeploy, or override for now:
     ```bash
     flyctl secrets set LLM_MODEL=openrouter/deepseek/deepseek-v4-flash --app <app-name>
     ```
   - `/readyz` surfaces this as `"llm_model": "warning: ..."` (informational —
     it does not fail readiness).
   - Otherwise the key itself is bad — rotate it (see Operations Runbook → *Credential Rotation*).

3. **Model not found (404)** — the model name in `llm.model` is incorrect or
   unavailable for that provider.

4. **Timeout** — the model took too long to respond. Check
   `budgets.command_timeout_seconds` (default 120) and
   `budgets.task_timeout_seconds` (default 900) in `config.yaml`; see *Hung
   call* above when one LLM call ate most of the budget.

**Resolution:**
- If rate limited: wait and retry (automatic backoff: 60s, 300s, 1800s)
- If API key expired: rotate the key (see Operations Runbook → *Credential Rotation*)
- If model unavailable: switch to a different model (keep the `openrouter/` prefix while the key is an OpenRouter key):
  ```bash
  flyctl secrets set LLM_MODEL=openrouter/anthropic/claude-sonnet-4-7 --app <app-name>
  ```

---

## Task Aborted Due to Budget Cap

**Symptoms:**
- Task has status `aborted_budget`
- A comment is posted on the issue explaining the abort
- Logs show `budget_exceeded`, `cost_limit_reached`, or `llm_cost_unavailable`
  with `action=task_aborted` (the `error` column reads `cost_unavailable`)

**Diagnosis:**
```bash
# Check the task details
flyctl ssh console --app <app-name> --command \
  "sqlite3 -header /data/issue_agent.db \"SELECT id, cost_usd, iterations, error FROM tasks WHERE status='aborted_budget' ORDER BY created_at DESC LIMIT 5;\""
```

**Probable causes** (1–3 from the `budgets:` block in `config.yaml`):
1. Per-task cost limit (`budgets.max_cost_per_task_usd`, default 2.00) was reached
2. Iteration limit (`budgets.max_iterations`, default 50) was reached
3. Task timeout (`budgets.task_timeout_seconds`, default 900s) was reached
4. `error=cost_unavailable`: `llm.cost_tracking` is `strict` (the default) and a
   step's cost could not be determined — the provider reported none and the model
   isn't in LiteLLM's price map. See *Cost Always Reported as US$ 0.00* below;
   switch `llm.cost_tracking` to `lenient` only if you accept unmetered runs.

**Resolution:**
- If the task legitimately needed more budget, raise the limit in
  `config.yaml` and redeploy, or override for now:
  ```bash
  flyctl secrets set MAX_BUDGET_PER_TASK_USD=5.00 --app <app-name>
  ```
- If the agent is stuck in a loop (high iterations with low cost), investigate the trajectory log at `/data/events/<task-id>/trajectory.json`

---

## Cost Always Reported as US$ 0.00

**Symptoms:**
- Every task's issue comment / `cost_usd` shows `US$ 0.00` despite real LLM spend
- `MAX_BUDGET_PER_TASK_USD` / `MAX_DAILY_BUDGET_USD` never trigger

**Cause:** the LLM cost comes from the provider's `usage.cost` (via
`CostAwareLitellmModel._calculate_cost`). If the provider doesn't return it and
the model isn't in LiteLLM's static price map, the cost of that step can't be
determined. With `llm.cost_tracking: lenient` this falls back to `0.0` and only
logs `llm_cost_unavailable`; with `strict` (the default) the task is aborted
(`error=cost_unavailable`) and a budget alert fires, rather than run unmetered.

```bash
flyctl logs --app <app-name> | grep llm_cost_unavailable
```

**Resolution:**
- Prefer an `openrouter/...` model — OpenRouter always returns `usage.cost`, so
  cost tracking works for any of its models with no price-map entry needed.
- For another provider, ensure the model is in LiteLLM's price map or register it
  via `LITELLM_MODEL_REGISTRY_PATH`.
- Only set `llm.cost_tracking: lenient` if you deliberately accept that budget
  guards won't fire for that model.
- Consider improving prompts to make the agent more efficient

---

## Git Clone / Push Failures

**Symptoms:**
- Task fails during workspace provisioning
- Logs show `git_clone_failed`, `git_push_failed`, or `authentication_failed`

**Diagnosis:**
```bash
flyctl logs --app <app-name> | grep "git_\|clone\|push\|auth"
```

**Probable causes and checks:**

1. **Installation token expired** — tokens have a 1-hour TTL. If the task takes longer than 1 hour from clone to push, the token may expire. Check the task duration.

2. **GitHub App not installed in the target repo** — verify installation:
   ```bash
   # This will fail with 401/404 if the app isn't installed
   curl -H "Authorization: Bearer <installation-token>" https://api.github.com/repos/<owner>/<repo>
   ```

3. **Repository permissions incorrect** — the GitHub App needs Contents: Read & Write permission on the target repo.

4. **Target repo doesn't exist or is misspelled** — the issue→code repo
   pairs live in `config.yaml` under `repos:` (`issues:` / optional
   `target:`), not in an env var:
   ```bash
   flyctl ssh console --app <app-name> --command \
     "python -c 'from issue_agent.config import get_settings; print(get_settings().repo_routes)'"
   ```

**Resolution:**
- Re-install the GitHub App in the target repository
- Verify the `repos:` block in `config.yaml` names the right repos, and
  redeploy (`allowed_repos` is derived from `repos[].issues`)
- If the task timing is the issue, consider reducing the complexity of the prompt

---

## Duplicate Comments or PRs

**Symptoms:**
- The agent posts multiple comments on the same issue
- Multiple PRs created for the same issue

**Probable causes:**

1. **Webhook event storm** — a single issue creation can generate multiple events (e.g., `issue.opened` → `label_added` → webhook re-delivery). The idempotency layer (`processed_deliveries`) should catch duplicates, but may not catch events with different delivery IDs that reference the same issue.

2. **Worker processed the same issue twice** — a partial unique index
   (`idx_tasks_unique_pending` on `(repo, issue_number, trigger_kind)`
   `WHERE status = 'pending'`) blocks a second *pending* task for the same
   trigger, but a completed task plus a new event for the same trigger can
   still create a new task.

3. **Platform re-delivery** — GitHub may re-deliver a webhook if it doesn't receive a 2xx response within 10 seconds.

**Diagnosis:**
```bash
# Check how many tasks exist for the same issue
flyctl ssh console --app <app-name> --command \
  "sqlite3 /data/issue_agent.db \"SELECT id, status, trigger_kind, created_at FROM tasks WHERE issue_number=<N> ORDER BY created_at;\""
```

**Resolution:**
- The core ingestion filter (`policy/ingestion.py::ingestion_filter`) drops
  `issue_comment` events authored by this app (`Author.is_self`, set by
  `providers/github/webhook.py`) and any comment on a **closed** issue. If the bot still appears to react to its own comments,
  confirm `providers.<id>.app_id` / `bot_login` match the app and check the logs for
  `ignored_own_comment` / `ignored_closed_issue_comment`. Comments from *other*
  bots are intentionally still processed.
- If duplicate PRs are created, verify the idempotency logic in `providers/github/pr.py` (checks for existing branch before creating PR)

---

## PR Still Conflicting After a Follow-up

**Symptoms:**
- A maintainer commented "resolve the conflicts" / "update the branch" and the
  PR still shows "This branch has conflicts that must be resolved"
- The issue got `could not bring the branch up to date with <base>`, or the
  fix comment carries "still reports conflicts"

**Probable causes:**

1. **The comment was posted on the PR, not on the issue.** Follow-ups are
   triggered only by comments on the *issue*; comments on the PR's
   Conversation tab are ignored (`routes.py`).
2. **The agent left conflict markers** — the run aborted with
   `error=merge_conflict_unresolved` and pushed nothing.
3. **The branch was pushed without a real merge** — e.g. a run before this
   harness version, which copied the base's changes in by hand. The PR's
   merge base never moved, so the copied lines conflict
   with the originals.

**Diagnosis:**
```bash
# What the base merge did on the run (merged / conflict + files / failed / unresolved)
flyctl logs --app <app-name> | grep '"task_id":"<task-id>"' \
  | grep -E 'branch_update_|base_merge_|branch_still_conflicting'

# Outcome of the run
flyctl ssh console --app <app-name> --command \
  "sqlite3 /data/issue_agent.db \"SELECT id, status, error FROM tasks WHERE id='<task-id>';\""

# Is the base an ancestor of the PR head? (run in any clone of the target repo)
git fetch origin <base> <work-branch>
git merge-base --is-ancestor origin/<base> origin/<work-branch> && echo merged || echo behind
```

**Resolution:**
- Comment again on the **issue**: the next run merges the base, hands the
  conflicted files to the agent and records a two-parent merge.
- If it keeps failing on the same files, resolve the merge by hand on the
  work branch (`git merge origin/<base>`, fix, commit, push); the next
  follow-up starts from there.
- On a forge with no PR API the same applies to the published branch: the
  warning comes from the harness's own `git merge-tree` check, since there is
  no PR to ask.

---

## Branch Published but No PR Opened

**Symptoms:**
- The run succeeded, the issue got a comment with a compare link
  ("No … was opened"), and no PR exists
- `tasks.pr_number` is empty and `issue_work.closed_reason` is
  `branch_published`

**Probable cause:** this is the expected delivery on a forge with no PR API
(`capabilities.supports_pr_api: false`). The harness pushes the work branch
and leaves opening the PR to a human.

**Diagnosis:**
```bash
flyctl ssh console --app <app-name> --command \
  "sqlite3 /data/issue_agent.db \"SELECT issue_repo, issue_number, work_branch, closed_reason FROM issue_work WHERE closed_reason='branch_published';\""
```

**Resolution:**
- Open the PR from the compare link in the comment.
- To ask for changes, comment on the **issue**: the next run reopens the work
  item and pushes onto the same branch.

---

## CI Monitor Says "No CI Detected" or Never Reports

**Symptoms:**
- `ci_monitor.enabled` is on, but a PR got "No CI checks were detected"
  although the repo has CI, or no CI comment at all.

**Probable causes:**
- The GitHub App lacks **Checks**, **Commit statuses** or **Actions** read
  access: the API reports nothing (or 403s, which the sweep logs as
  `ci_monitor_row_failed` and retries forever).
- The workflows don't run on the agent's PRs: draft PRs or bot-authored
  pushes filtered out (`if: github.event.pull_request.draft == false`,
  `pull_request` paths/branches filters), or the workflow is awaiting
  approval to run.
- CI registered its first check later than `ci_monitor.no_ci_grace_minutes`.
- `repos[].ci_monitor: false` on that repo, or the forge has no CI port.

**Diagnosis:**
```bash
flyctl ssh console --app <app-name> --command \
  "sqlite3 /data/issue_agent.db \"SELECT issue_repo, issue_number, pr_number, head_sha, state, created_at, last_polled_at FROM ci_watch ORDER BY created_at DESC LIMIT 20;\""
flyctl logs --app <app-name> | grep ci_monitor
```

**Resolution:**
- Grant the missing permissions in the App settings and accept them on each
  installation (GitHub asks the installation owner to approve new
  permissions).
- Raise `ci_monitor.no_ci_grace_minutes` for a repo whose CI is slow to start.
- To re-check one commit, set its row back to watching:
  `UPDATE ci_watch SET state='watching', created_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE issue_repo='<owner/name>' AND issue_number=<n>;`

---

## Disk Space Low on Volume

**Symptoms:**
- Workspace creation fails with "no space left on device"
- SQLite write operations fail
- Tasks can't start

**Diagnosis:**
```bash
# Check disk usage
flyctl ssh console --app <app-name> --command "df -h /data"

# Check what's taking space
flyctl ssh console --app <app-name> --command "du -sh /data/* | sort -rh"

# Check event log sizes
flyctl ssh console --app <app-name> --command "du -sh /data/events/* | sort -rh | head -10"
```

**Resolution:**

1. **Clean up old event logs** — remove events older than retention period:
   ```bash
   flyctl ssh console --app <app-name> --command \
     "find /data/events -mindepth 1 -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +"
   ```

2. **Remove stale workspaces** (from orphaned tasks that weren't cleaned):
   ```bash
   flyctl ssh console --app <app-name> --command \
     "find /data/workspaces -mindepth 1 -maxdepth 1 -type d -mtime +1 -exec rm -rf {} +"
   ```

3. **Compact the SQLite database** (reclaims space from deleted rows):
   ```bash
   flyctl ssh console --app <app-name> --command \
     "sqlite3 /data/issue_agent.db 'VACUUM;'"
   ```

4. **Increase volume size** (requires Machine restart):
   ```bash
   flyctl volumes extend <volume-id> --size 5
   ```

---

## Agent Produces Incorrect PR

**Symptoms:**
- A PR is created but the changes are wrong or incomplete
- The fix doesn't actually solve the issue
- Tests fail after applying the changes

**Diagnosis:**

1. **Review the trajectory log** — the complete history of the agent's actions is saved:
   ```bash
   flyctl ssh console --app <app-name> --command "cat /data/events/<task-id>/trajectory.json"
   ```

2. **Check what files were modified and why** — the PR body includes the list of changed files and the agent's reasoning.

3. **Determine if the issue was too ambiguous** — check if the agent asked clarifying questions (it should have stopped and asked instead of guessing).

**Probable causes:**
- **Insufficient context in the issue** — the issue body didn't describe the problem clearly enough
- **Prompt quality** — the system prompt may need adjustments for the specific type of issue
- **Model limitations** — the current LLM may not be well-suited for the task (e.g., deep codebase understanding)
- **Missing CLAUDE.md or project conventions** — the agent relies on `CLAUDE.md` in the target repo for project-specific guidance

**Resolution:**
1. Improve the issue description before re-triggering the agent
2. Refine prompts in `prompts/system.md` (for generic improvements) or the relevant template file in `prompts/`
3. Try a different LLM model (keep the `openrouter/` prefix while the key is an OpenRouter key):
   ```bash
   flyctl secrets set LLM_MODEL=openrouter/anthropic/claude-sonnet-4-7 --app <app-name>
   ```
4. Ensure `CLAUDE.md` exists in the target repo with accurate project conventions
5. Add the `agent-skip` label to the issue and handle it manually if the fix is too complex
