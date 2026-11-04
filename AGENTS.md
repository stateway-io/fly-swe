# fly-swe — Issue Agent

## Project Overview

**Issue Agent**: mini-SWE-agent service on Fly.io that listens to GitHub webhooks, triages issues, and opens PRs with fixes.

Start with the [README](./README.md) for the pitch, the end-to-end flow, and
the deploy quickstart, then [`docs/operations.md`](./docs/operations.md) for
operational depth. The [PRD](./PRD.md) describes the provider-plugin
architecture (Phases 0–4 implemented); the source
under `src/` and the docs above are the authoritative reference.

## Stack

Python 3.12 | uv 0.11+ | FastAPI | mini-SWE-agent 2.4.6 | SQLite WAL | LiteLLM | structlog | PyGithub | Docker (slim) | Fly.io Machines | honcho

## Directory Structure

```
src/issue_agent/
├── main.py             → FastAPI entry
├── worker.py           → worker loop
├── config.py           → Pydantic Settings
├── logging.py          → structlog setup
├── core/               → canonical model (TriggerEvent, IssueRef, Author, Comment), PR text (pr_text.py)
├── providers/          → provider ports (base.py), registry, per-instance settings union (settings.py), github/ (settings, webhook adapter, App auth, API client, PR ops, git transport)
├── webhook/            → generic dispatcher POST /webhooks/{instance_id}, health
├── queue/              → SQLite store, models, schema
├── policy/             → label router, guards, ingestion filters, comment-author filter
├── runner/             → workspace, agent_factory, prompts, executor, diagnose, toolchains/
├── triage/ diagnose/   → triage and diagnosis comment builders
└── observability/      → audit log, metrics, alerts, cleanup
prompts/                → system.md, system_diagnose.md + templates per label (forge-neutral, see Provider plugins)
tests/                  → unit, integration, e2e
config.yaml             → non-secret configuration (precedence: env > yaml > default)
Dockerfile / fly.toml / Procfile
docs/                   → runbook (operations), troubleshooting, label-schema, deploy/, plans/, research/
```

## Architecture Constraints

- **Single Fly Machine** — webhook + worker in the same process via honcho (see `Procfile`, `fly.toml`)
- **GitHub App** for auth, not a PAT (`src/issue_agent/providers/github/auth.py`)
- **`LocalEnvironment`** from mini-SWE-agent, scoped, not a Docker sandbox — see the README's "Status, limitations & roadmap" for the sandboxing plan
- **SQLite + persistent Fly Volume**, not Postgres (`src/issue_agent/queue/`)
- **Triple cost controls**: `max_iterations`, `max_cost_per_task_usd`, `max_daily_cost_usd` (`config.yaml.example` → `budgets:`)
- **Never merge PRs** — a human always reviews

## Implementation

Follow the git convention below and the invariant in
[`docs/operations.md`](./docs/operations.md) § The Agent Execution Contract:
the coding agent edits files and leaves the changes in the working tree; the
harness owns git (branch, commit, push, PR). Commits go on your feature
branch `feat/<slug>` (`main` is protected for PRs only).

## Build & Test

```bash
uv sync                              # install deps
uv run pytest                        # run all tests
uv run pytest tests/unit/test_config.py -v  # specific test
uv run ruff check && uv run ruff format --check   # lint + format (CI gate)
uv run pytest tests/unit tests/integration --cov  # coverage (CI gate, floor in pyproject.toml)
uv run honcho start                  # run locally (web + worker)
LOG_LEVEL=debug uv run pytest -v     # debug
```

## Test Discipline

- **Every implemented piece of code must have corresponding tests** in the appropriate directory (`tests/unit/`, `tests/integration/`, `tests/e2e/`).
- Tests are written with `pytest`, not as disposable scripts (`/tmp/`, inline in CLI, etc.).
- Production scenarios (without `.env`/`config.yaml`) must be simulated with a subclass that nullifies `env_file`/`yaml_file`, not with fragile `monkeypatch`.
- All implementations must pass `ruff check`, `ruff format --check` and `uv run pytest` before opening a PR.
- Coverage (unit + integration, branch) must stay at or above the `fail_under` floor in `pyproject.toml` (90%); CI fails below it. Don't lower the floor to land a change — add tests.
- Tests that depend on a real LLM or network go in `e2e/` and are opt-in.

## Deploy

```bash
export FLY_APP=<app-name>            # fly.toml has no `app` field
flyctl deploy --remote-only          # fly.toml is at the root
flyctl logs                          # view logs in prod
```

## Git Convention

### Commits and PRs — Mandatory Standard

Both commits and PR titles follow **Conventional Commits** with an optional
reference to a tracking issue:

```
type: concise description [#issue]
```

| Type | Usage |
|---|---|
| `feat` | New functionality |
| `fix` | Bug fix |
| `docs` | Documentation (AGENTS.md, README, docs/, etc.) |
| `refactor` | Refactoring with no behavior change |
| `perf` | Performance improvement with no functional change |
| `test` | Adding or fixing tests |
| `chore` | Maintenance (deps, CI, config, tooling) |
| `ci` | Changes to CI/CD pipelines |

**Rules:**
- `[#issue]` references the GitHub issue this closes or advances, when there is one
- Commit and PR title must have the **same text** (unless the PR groups multiple commits)
- Commits in English
- Atomic commits: one commit per relevant piece of progress (don't batch up)

Examples:
```
feat: implement runner/prompts.py [#4]
fix: validate HMAC on empty payload [#1]
docs: add troubleshooting for Fly volume [#6]
refactor: extract label router to policy module [#5]
chore: bump mini-swe-agent to 2.4.6 [#2]
ci: add ruff check to CI pipeline [#6]
```

### Agent branches (bot — in the target repository)

- `agent/issue-<n>-<slug>`
- Commits: `fix(#<issue>): <imperative message in English>` + co-author
- Agent PRs: label `agent-generated`

### Development branches (in this repository) — trunk-based

`main` is the **only** long-lived branch. Protected (PR + `ci-ok` + 1 review,
`strict` enabled — the branch must be up to date with `main` before merging).
Never push directly.

Feature branches are short-lived, always created **from `origin/main`**, and the
prefix matches the Conventional Commit `type`:

| Prefix | Usage |
|---|---|
| `feat/<slug>` | New functionality |
| `fix/<slug>` | Bug fix |
| `docs/<slug>` | Documentation |
| `refactor/<slug>` | Refactoring |
| `perf/<slug>` | Performance |
| `test/<slug>` | Tests |
| `chore/<slug>` | Maintenance (deps, config, tooling) |
| `ci/<slug>` | CI/CD pipelines |

Merging the feature into `main` is a **squash**.

**Release flow:** merging into `main` does **not** trigger a deploy — every PR
already bumps `version` in `pyproject.toml` as part of itself (Feature workflow
step 5 below), so `main`'s version is always current and ready to tag. When a
set of changes is ready for production:
1. Confirm `main`'s current `pyproject.toml` version is the one to ship
2. Create a GitHub Release with tag `vMAJOR.MINOR.PATCH` **equal** to
   the `pyproject.toml` version: `gh release create vX.Y.Z --target main --generate-notes`
3. `deploy.yml` triggers on the Release and deploys to Fly + smoke tests
4. Manual rollback / redeploy: `workflow_dispatch` on the **Deploy** workflow

**Feature workflow:**
1. `git fetch origin` — fetch the remote state
2. `git checkout -b feat/<slug> origin/main` — create the branch from the
   **current `origin/main`** (never from local `main`, which may be stale)
3. `git status --porcelain` — verify the tree is clean
4. Implement with atomic commits (standard above)
5. **Bump `version` in `pyproject.toml`** (semver: `patch` = fix/perf, `minor` = feat,
   `major` = breaking — matching the highest-ranking Conventional Commit type
   in the branch) and run `uv lock` to sync `uv.lock`, as its own
   `chore: bump version to X.Y.Z` commit. Every PR into `main` bumps the
   version itself — don't defer it to a follow-up PR.
6. When the session's work is finished (checks above green), push the feature
   branch: `git push -u origin <type>/<slug>`. Pushing a feature branch needs
   no extra permission — it never touches `main`, and the human reviews the
   commits there.
7. **Never open a PR without explicit permission** — ask first; once
   authorized, open it to `main` with the same title as the commit

## Key Security Rules
- Secrets in `fly secrets set` / env (`PROVIDERS__<ID>__*` for provider instances), **never** in the repository nor in `config.yaml` — the loader rejects them there (see `docs/operations.md` § Configuration)
- Container runs as a **non-root user** (UID 1000)
- Agent workspace: only `/data/workspaces/<task-id>/<owner>/`
- The issue body is **untrusted data**: it is not interpreted as a template, and the agent's subprocess runs with a minimal env with no secrets (`runner/environment.py`, `runner/prompts.py`)
- Timing-safe HMAC SHA-256 on every webhook, failing closed on an empty secret (`providers/github/webhook.py`)

## Provider plugins

Every forge (GitHub today) is a plugin behind the ports in
`src/issue_agent/providers/base.py`; nothing outside `providers/` imports a
forge SDK or names a forge.

- A plugin implements `ProviderPlugin`: `capabilities`, `webhook`
  (`WebhookAdapter`: signature, delivery key, `parse_event` → canonical
  `TriggerEvent`), `client(repo)` (`ForgeClient`), `pulls(repo)`
  (`PullRequestClient`, or `None` on a forge with no PR API),
  `transport()` (`GitTransport`), `bot_identity()`, `health_check()`.
- Instances come from config (`providers.<id>`); each one gets its own
  webhook URL (`/webhooks/<id>`), credentials (`PROVIDERS__<ID>__*`) and
  `/readyz` checks. `ProviderRegistry` (`providers/registry.py`) is injected,
  never a global.
- `capabilities.terms` is the forge's vocabulary. Prompts and the text the
  core writes use it (`{{ terms.issue }}`, `{{ terms.pr }}`,
  `{{ instructions_files }}`) — never write "GitHub", "Pull Request" or "PR"
  in `prompts/` (`tests/unit/test_prompts.py` enforces it).
- Metrics and audit entries carry `provider_instance` next to `repo`.
- Adding a forge means: a package under `providers/<type>/`, its settings
  model in the union in `providers/settings.py`, an entry in
  `BUILTIN_PROVIDER_TYPES`, and a kit in `tests/unit/providers/kits.py` so
  the conformance suite (`tests/unit/providers/test_provider_contract.py`)
  runs against it. Core tests use `tests/fakes/fake_provider.py`.
- The contributor guide for writing a provider is
  [`docs/providers.md`](./docs/providers.md); keep it in sync when a port changes.

## Toolchains (on-demand runtime provisioning)

The base Docker image is Python-only. When a target repo needs another runtime
(e.g. Node.js for TypeScript/Jest projects), the harness detects it from
project files, downloads the official upstream binary into `/data/tools` on
the persistent Fly volume, and reuses it across tasks. No sudo or system
package installation is required.

- Detection order: `.nvmrc` → `package.json` `engines.node` → `volta.node` → default LTS
- Supported lockfiles for dependency install: `package-lock.json` (`npm ci`),
  `yarn.lock` (`yarn install --frozen-lockfile`), `pnpm-lock.yaml` (`pnpm install --frozen-lockfile`)
- Configure per repo with `repos[].project.toolchain` (`auto`/`nodejs`/`none`) and
  `repos[].project.node_version`
- Global knobs under `toolchains:` in `config.yaml` (`tools_root`,
  `nodejs.default_version`, `install_timeout_seconds`, etc.)
- Adding a new stack means implementing `ToolchainPlugin` and registering it in
  `src/issue_agent/runner/toolchains/registry.py`
