# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for a security vulnerability.
Instead, email **samuel@stateway.io** with a description of the issue and, if
possible, steps to reproduce it. Expect an initial response within a few
days.

## Threat model

fly-swe runs an LLM-driven agent against **untrusted input**: the body of a
GitHub issue, and comments on it, written by anyone who can open an issue on
a repo the agent watches. This is the honest picture of what that agent can
and cannot reach today.

### What is NOT sandboxed

The agent's shell commands run via `subprocess` **inside the same container**
as the web server and worker — not in a separate sandbox or ephemeral VM.
There is no `DockerEnvironment`, no per-task Fly Machine, no gVisor/Firecracker
boundary beyond the one Fly already puts around the whole service. A command
the agent is tricked into running has, in principle, the same reach as the
service process itself.

**A sandboxed execution environment is on the roadmap** — see the README's
*Status, limitations & roadmap* section. Until then, treat the mitigations
below as defence-in-depth, not as a sandbox.

### Mitigations that do exist

- **No secrets in the agent's shell.** The subprocess environment is built
  from an explicit allowlist (`runner/environment.py`), not inherited from
  the worker process — the GitHub App key, LLM API key, and `FLY_API_TOKEN`
  are never visible to a command the agent runs.
- **Tokenless git remote.** The clone URL never carries credentials
  (`runner/git_auth.py`); there is nothing in `.git/config` for the agent to
  read and exfiltrate.
- **No push credential, no `gh`.** The agent cannot push, open a PR, or call
  the GitHub API directly — only the harness can, after the agent's run ends
  and `git status --porcelain` confirms uncommitted local changes.
- **Untrusted text is never interpreted as a template.** Issue/comment/resume
  content enters the prompt as inert Jinja *values* inside explicit markers
  (`runner/prompts.py`), never concatenated into template source — a `{{ …
  }}` or `{% … %}` sequence in an issue body renders as literal text.
  `tests/unit/test_prompts.py` locks this.
- **Explicit prompt-level constraints** (`prompts/system.md`): no reading
  `.env` / `.git/config` / `~/.ssh` / anything matching `*KEY*`/`*SECRET*`,
  no `env`/`printenv`, no arbitrary outbound network calls, no `sudo`, no
  touching `.github/workflows/`.
- **Per-command and per-task limits.** Every shell command the agent runs is
  killed (whole process group) at `command_timeout_seconds`; the task itself
  is capped by `max_iterations`, `max_cost_per_task_usd`, and
  `task_timeout_seconds` — see `config.yaml.example` → `budgets:`.
- **A human always reviews.** The agent never merges its own PR.
- **Fly's own VM isolation.** Whatever the mitigations above miss, the
  service still runs inside a Fly Machine's Firecracker VM, not bare metal.

### What we ask of operators

- Only install the GitHub App on repositories you trust to receive automated
  PRs, and restrict who can label an issue for the agent to act on
  (`policy/comment_filter.py`'s author filter, or your repo's own
  permissions).
- Review every agent-generated PR before merging, as you would any
  contribution from an untrusted source.
- Keep `LLM_API_KEY`, `PROVIDERS__<ID>__APP_PRIVATE_KEY`, and
  `PROVIDERS__<ID>__WEBHOOK_SECRET` in Fly secrets (or your platform's
  secret store) — never in `config.yaml` (where the service rejects them) or
  the repository.

## Supported versions

Only the latest released version on `main` receives security fixes.
