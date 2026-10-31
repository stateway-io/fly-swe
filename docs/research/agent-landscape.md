# Research: reusable agents for issue handling and deploy on Fly.io

> **Non-normative research snapshot.** This document records the engine
> evaluation and landscape survey as of the date below; it is not kept in
> sync with the codebase. For current, authoritative behaviour see
> [`docs/operations.md`](../operations.md) and the source under `src/`.

**Version:** 1.0
**Date:** 2026-09-03
**Status:** Historical snapshot
**Complements:** [`docs/research/mini-swe-agent-api.md`](./mini-swe-agent-api.md) (engine API reference)

---

## Table of Contents

1. [Context and method](#1-context-and-method)
2. [Current engine — what the update itself resolves](#2-current-engine--what-the-update-itself-resolves)
3. [Landscape of reusable engines (Sep/2026)](#3-landscape-of-reusable-engines-sep2026)
4. [Ready-to-use agents — products, not engines](#4-ready-to-use-agents--products-not-engines)
5. [Trigger model: App + webhook vs Actions](#5-trigger-model-app--webhook-vs-actions)
6. [Execution and sandbox on Fly.io](#6-execution-and-sandbox-on-flyio)
7. [Security and prompt injection](#7-security-and-prompt-injection)
8. [Licensing and SaaS viability](#8-licensing-and-saas-viability)
9. [Recommendation and backlog](#9-recommendation-and-backlog)
10. [Sources](#10-sources)

---

## 1. Context and method

### 1.1 Why update

The choice of the Issue Agent's execution engine was documented in two internal research reports (not published) **frozen as of May/2026**:

| Document | Target | Version studied | Date | Role today |
|---|---|---|---|---|
| OpenHands SDK study | OpenHands Software Agent SDK V1 | v1.22.1+ | May/2026 | Historical reference |
| mini-SWE-agent study | mini-SWE-agent v2 | 2.2.8 | 2026-05-21 | Active API reference |

Four months later, four things changed materially:

1. **mini-SWE-agent** advanced from 2.2.8 to **2.4.6** (Jul/2026), with fixes that hit real bugs in our code.
2. **OpenHands SDK** jumped from v1.22.1 to **v1.44.1** (Aug/2026) — the "still in V0→V1 transition" argument that motivated the pivot has aged.
3. **Fly.io launched Sprites** (Jan/2026), changing the default sandbox design for agents on the very platform we already use.
4. There were **public prompt injection incidents** in agents that read issues — exactly this project's attack surface.

The survey covers **two distinct layers**, which are frequently confused:

| Layer | Question it answers | Section |
|---|---|---|
| **Engines** (libraries to embed) | Which agent loop runs inside our service? | §3 |
| **Ready-made agents** (installable products) | What already exists ready to use, that gets parameterized and deployed, without writing the agent layer? | §4 |

### 1.2 State of the audited code

Base: commit `89f1b38` (`main`). At the time the package lived in `agents/issue-agent/`;
today it's at the repo root.

| Fact | Where |
|---|---|
| Engine: mini-SWE-agent, pin `>=2.2.8,<3.0.0`, locked at **2.2.8** | `pyproject.toml:17`, `uv.lock` |
| LiteLLM 1.85.0, enters transitively (but is imported directly) | `uv.lock`, `runner/litellm_cost.py` |
| Three engine classes in use: `DefaultAgent`, `LitellmModel`, `LocalEnvironment` | `runner/agent_factory.py:5-7` |
| Engine-swap interface defined, **with no production implementation** | `runner/delegators/__init__.py` (29 lines, pure protocol) |
| Pipeline: webhook → SQLite (WAL) → worker polling 2s → executor → PR | `webhook/routes.py`, `queue/store.py`, `worker.py`, `runner/executor.py` |
| Deploy: **one** Fly Machine (`shared-cpu-1x`, 1 GB, `gru`), honcho forking `web` + `worker`, `/data` volume | `fly.toml`, `Procfile`, `Dockerfile` |
| Deploy triggers on published GitHub Release (trunk-based) | `.github/workflows/deploy.yml` |

### 1.3 Method and source reliability

Web survey on 2026-09-03. Sources classified:

- **Primary (high confidence):** GitHub release notes, official docs (`docs.openhands.dev`, `code.claude.com`, `fly.io/docs`), PyPI, GitHub changelog, arXiv.
- **Secondary (medium):** security vendor blogs (Microsoft, CSA, Aikido) about incidents.
- **Tertiary (low — treat as a lead):** blog and aggregator comparisons, especially pricing and benchmark numbers published by direct competitors of the platforms being compared. Where used, they are flagged in the text.

> ⚠️ SWE-bench numbers cited in tertiary sources mix *scaffold* and *model*. The same harness varies by dozens of points depending on the model behind it. Use them to order magnitudes, never to decide.

---

## 2. Current engine — what the update itself resolves

This is the report's highest practical-value section: **most of the available gain doesn't require switching engines, it requires upgrading the version of the engine we already use.**

### 2.1 Releases 2.3.0 → 2.4.6

| Release | Date | Change | Corresponding gap in our code |
|---|---|---|---|
| **2.3.0** | May/2026 | Native **wall-clock** limits to terminate the agent | Today the timeout is a daemon thread + `t.join(timeout=…)` in `runner/executor.py:62-72`. On overrun, `MiniSWERunner.run` returns, but **the thread stays alive** inside the process — continuing to spend tokens and writing to the workspace that `ws.cleanup()` will delete |
| **2.4.0** | Jun/2026 | Exposes `finish_reason`; limits consecutive format errors | Closes the most serious bug: `DefaultAgent.run()` internally catches `LimitsExceeded` and returns a dict with `exit_status`. `MiniSWERunner.run` **ignores this return value** and returns `AgentResult(success=True, …)` (`runner/executor.py:82-86`). Result: a budget overrun becomes a commit + push + PR with partial state. The branch `if result.error in ("budget_exceeded", "task_timeout")` (`runner/executor.py:276`) **never sees** `"budget_exceeded"` — nothing in the code produces that string |
| **2.4.1** | Jun/2026 | Kills the *process group* on timeout | Orphaned commands surviving the timeout in the workspace |
| **2.4.2** | Jun/2026 | ProgramBench v6 images | Irrelevant to us |
| **2.4.3** | Jun/2026 | Fixes `startup_command`; rejects empty override keys | Config robustness |
| **2.4.4** | Jul/2026 | Fixes **config mutation across worker threads**; removes an emoji that broke import under a non-UTF-8 locale | Relevant: we run the agent inside a thread and in a slim container |
| **2.4.5** | Jul/2026 | Reads config as UTF-8 regardless of locale; better handling of malformed tool calls | Robustness |
| **2.4.6** | Jul/2026 | **Failed API calls now count against `cost_limit`** | Directly reinforces the cost control that already failed once in production (cost tracking silently reported `cost_usd = 0.0`) |

### 2.2 Reading

The pin `mini-swe-agent>=2.2.8,<3.0.0` allows 2.4.6 — what's locking it down is `uv.lock`. **It's a `uv lock --upgrade-package mini-swe-agent`, not a migration.**

The two highest-impact defects in the runner (budget overrun reported as success; abandoned thread on timeout) stop requiring our own workaround once the engine exposes `finish_reason` and native wall-clock limits. Today we maintain workaround code for functionality upstream already delivers.

**Estimated cost:** 1 small PR — lock bump, `MiniSWERunner.run` starts inspecting the return value of `agent.run()`, tests covering `budget_exceeded` and `task_timeout`.

---

## 3. Landscape of reusable engines (Sep/2026)

### 3.1 Comparison

| Engine | License | Version (Sep/26) | Embeddable as a lib | Native sandbox | Cost/step control | API surface | Fit for this project |
|---|---|---|---|---|---|---|---|
| **mini-SWE-agent** | MIT | 2.4.6 | Yes, trivial | No (`LocalEnvironment`); `DockerEnvironment` available | `cost_limit`, `step_limit`, wall-clock (≥2.3.0) | Minimal (3 classes) | **Incumbent. Keep.** |
| **OpenHands Software Agent SDK** | MIT | v1.44.1 | Yes, but heavy (4 packages) | Yes: `DockerWorkspace`, `RemoteAPIWorkspace`, agent-server | Cumulative cost in callbacks; LLM pre-flight validation | Large (Tool framework, events, security analyzer, hooks, pause/resume) | Candidate for **optional delegator**, not a loop replacement |
| **SWE-agent** | MIT | 1.1.0 (May/2025) | Yes | Docker | Yes | Medium | **Discard** — development moved to mini |
| **Claude Agent SDK** | Proprietary (Anthropic's SDK/CLI) | tracks Claude Code | Yes (Python/TS, headless mode) | Not on its own; inherits the host's | Permissions, `--allowedTools`, cost tracking | Medium-high (MCP, subagents, hooks) | **Default delegator in the original roadmap** — better quality per task, worse license/BYOK story |
| **open-swe (LangChain Deep Agents)** | MIT (server requires a license) | active, ~10.7k★ | Partially | Pluggable: E2B, Modal, Daytona, Runloop, LangSmith, local | Yes, via LangGraph | Large (LangGraph, 5 graphs) | **Design reference**, not code — production requires a licensed LangGraph Server |
| **Goose** | Apache 2.0 (Agentic AI Foundation / Linux Foundation) | active | Yes | No | Yes | Medium | Weak fit: designed for interactive/desktop use |
| **Aider** | Apache 2.0 | active | No (CLI) | No | Yes | CLI | Weak fit for non-interactive server execution |

### 3.2 What changed in each candidate

**mini-SWE-agent (incumbent).** The original engine-choice thesis holds up: MIT, academic maintainers (SWE-bench authors), ~100-line core, bash only, native cost tracking via LiteLLM, linear history that matches our append-only audit log. The eight releases since 2.2.8 are all about robustness — no API breakage within the `2.x` range. **A healthy signal of maintenance without bloat.**

**OpenHands SDK.** Grew from v1.22.1 to v1.44.1 in ~3 months — 22 minor releases. It gained things we're currently missing: first-class Docker/remote workspaces, a security analyzer, cumulative cost in automation callbacks, LLM pre-validation endpoints, credential redaction in errors. The release pace, however, **confirms** the pivot's second reason ("expected breaking changes"): it's a fast-moving target. It's still MIT and still the best option if we ever need a ready-made remote sandbox without building one.

**SWE-agent (the "big one").** Last release 1.1.0, May/2025. The project itself points to mini. **Closed as an option.**

**Claude Agent SDK.** It's the same Claude Code loop, program-driven, with a headless mode (`-p`, `--output-format stream-json`, `--allowedTools`, `--permission-mode`) designed for unattended pipelines. Anthropic documents internal use for issue triage. Strong point: quality per task. Weak point for us: **it's not open source, and cost/licensing rides on the Anthropic account** — direct friction with "opening up to the community" and with multi-provider BYOK (today we go through OpenRouter).

**open-swe.** It's the market project most similar to ours: GitHub App, tasks started from issues, PR review, pluggable sandbox, persistent thread. Worth reading the architecture (Agent/Reviewer/Analyzer/Chat/Scheduler separation) as a catalog of future features. Not worth adopting: it couples to LangGraph and the production server is licensed.

**Meta-harnesses.** In 2026 a category of platforms emerged that run *multiple* harnesses under one common shell (Tembo pattern; Omnigent/Databricks, Apache 2.0, Jun/2026). This **validates the `CodingAgentDelegator` bet**: the engine became a commodity; the value is in orchestration (queue, label policy, cross-task budget, auditing, recovery, GitHub App). It's exactly the layer this repo already has and that none of the engines above deliver.

### 3.3 Benchmark, with a caveat

Orders of magnitude publicly reported in 2026: frontier models above 85% on SWE-bench Verified with their own scaffold; open source scaffolds (OpenHands, mini-SWE-agent) in the **70-76%** range depending on the model behind them. The difference between the two open source scaffolds is smaller than the variation caused by switching the model.

**Implication:** swapping mini-SWE-agent for OpenHands doesn't buy measurable performance. Swapping the model does. Our lever is `LLM_MODEL`, not the engine.

> **Update (2026-10-18):** the SWE-bench Verified leaderboard has since saturated and stopped updating. For choosing `LLM_MODEL`, see [`coding-benchmarks.md`](./coding-benchmarks.md).

---

## 4. Ready-to-use agents — products, not engines

Section 3 evaluates **engines** (libraries to embed). This section answers the complementary question: **what already exists ready to use, that gets installed and parameterized, without writing the agent layer?**

### 4.1 Criteria for "ready"

Three filters, applied in this order:

1. **Installs without writing agent code** — App, Action, or container, not an SDK.
2. **Parameterizable via file/env** — labels, prompt, model, policy, no fork.
3. **Hostable on our own infra** — ideally on a Fly Machine, which is our deploy target.

Item 3 is what eliminates most candidates: almost everything that's ready-made is either closed SaaS, or runs on the customer's GitHub Actions.

### 4.2 The four categories

| Category | Deploy | Who pays for compute | Reuse for us |
|---|---|---|---|
| **A — Closed SaaS** | None | The vendor (subscription) | Zero. It's the competition |
| **B — GitHub Actions** | Commit a YAML | The customer (Actions minutes) | High in UX, null in server code |
| **C — Self-hostable** | Container/Helm on your infra | You | What matters — and it's the emptiest category |
| **D — Command-line tool** | No server | You, manually | Not a ready-made agent; it's the engine from section 3 |

### 4.3 Category A — Closed SaaS (zero deploy)

| Product | Trigger | Note |
|---|---|---|
| **GitHub Copilot coding agent** | Assign the issue to Copilot | Creates a branch, runs tests, iterates, opens a PR. Sandbox in Actions, native integration. It's the default competitor of any issue bot |
| **Google Jules** | Issue/prompt | Hosted |
| **OpenAI Codex Cloud** | Task/issue connected to the repo | Hosted |
| **Devin, Codegen, Tembo** | Issue, Linear, Slack | Agent platforms with sandboxed VMs |
| **OpenHands Cloud** (`app.all-hands.dev`) | `openhands` label or `@openhands` comment on the issue | Hosted version of OpenHands. The agent comments that it's working, opens a PR if it resolves it, and summarizes the run on the issue |
| **Sentry Seer / Autofix** | **Production error**, not an issue | Trigger different from ours; evaluates "fixability" before opening a PR. Billed per active contributor |
| **Sweep** | — | **Out of the game:** the repository declares a pivot to a JetBrains plugin. The most cited issue→PR bot of 2023-24 is no longer the product |

**Reading:** the market's most populous category is precisely the one we can't reuse. And the Copilot coding agent sets the expectation floor: *assigning the issue and getting a PR back* is now native platform behavior.

### 4.4 Category B — ready-made via GitHub Actions (deploy = one YAML)

| Product | License | Trigger | Parameterization |
|---|---|---|---|
| **`anthropics/claude-code-action@v1`** | Open action; proprietary model | `@claude` on issue/PR | Prompt, allowed tools, `CLAUDE.md` in the repo |
| **`openai/codex-action@v1`** | Open action; proprietary model | Workflow | Installs the Codex CLI on the runner and runs `codex exec` headless behind an API proxy |
| **OpenHands resolver** | MIT (inside the OpenHands monorepo) | `fix-me` label on the issue | `.openhands_instructions` versioned in the target repo |
| **`github/gh-aw`** | Open (technical preview) | Markdown workflows | Ready-made catalog, incl. issue auto-triage with a report in Discussion |
| **PR-Agent (as an Action)** | MIT | Commands on PR | `.pr_agent.toml` |

**Trade-off of this category:** trivial install and easy community adoption; in exchange, compute runs on the runner and the customer's budget, the LLM key belongs to the customer, charging per use becomes hard, and the prompt injection surface becomes the customer's CI/CD — exactly where the 2026 incidents happened (§7.2).

### 4.5 Category C — ready-made and self-hostable

This is the direct answer to the question. It's a short list.

| Product | License | Does issue→PR? | How it's hosted | Runs on a Fly Machine? |
|---|---|---|---|---|
| **PR-Agent** | **MIT** — donated by Qodo to the community, in the process of being donated to a foundation | **No** — operates only on PRs (`/describe`, `/review`, `/improve`, `/ask`, `/update_changelog`) | Webhook server in a container, or Action, or CLI. Config in `.pr_agent.toml`/`configuration.toml`; multi-provider via LiteLLM (includes OpenRouter and Ollama); supports GitHub, GitLab, Bitbucket, Azure DevOps, Gitea | **Yes, no drama** — it's a web service + container |
| **OpenHands (core)** | **MIT** | Yes, via resolver (Action mode) or interactive use | Container `ghcr.io/openhands/...`; the execution runtime is a container sandbox | Hard: needs to orchestrate nested containers |
| **OpenHands Cloud (self-hosted)** | **Polyform Free Trial — not open source.** Usage limited to 30 days per calendar year without a commercial license | Yes, it's the full product (App, multi-repo, UI) | **Helm charts on Kubernetes** (the same ones that run `app.all-hands.dev`) | **No** — assumes a k8s cluster |
| **open-swe** (LangChain) | MIT in the code; **production requires a LangGraph Agent Server license** | Yes — App, issues, PR review, scheduling | `make dev`/`make web` in development; production with LangGraph Server + a sandbox provider (LangSmith, Modal, Daytona, Runloop, E2B) | Not trivial: Node + licensed server + third-party sandbox |
| **Tabby (+ Pochi agent)** | Apache 2.0 | Peripheral — the core is a code completion server | Container | Likely, but outside the use case |

> ⚠️ The Tabby/Pochi row comes from a tertiary source and was not verified in the repository. Treat it as a lead, not a fact.

**The pattern that emerges:** what's free isn't ready to operate multi-repo in production (OpenHands core, resolver in Action); what's ready to operate multi-repo isn't free (OpenHands Cloud under Polyform + k8s; open-swe with a licensed server). And the only truly trivially self-hostable one — PR-Agent — **doesn't do issue→PR**.

### 4.6 The special case: our own engine already had this

mini-SWE-agent **v1 had** a ready-made issue runner: `run/github_issue.py`, which fetched the issue text by URL and ran the agent on top of it. **It was removed in v2** — the migration guide says to use the `mini` CLI instead.

Verified locally and upstream:

- `src/minisweagent/run/` on `main` does not contain `github_issue.py`;
- in the 2.2.8 installed in `.venv`, the entry points are only `mini`, `mini-e`, `mini-extra` and `mini-swe-agent`, and `run/extra/` is empty;
- `mini-extra`'s subcommands are `config`, `inspect`, `swebench`, `swebench-single` and `programbench` — none touches GitHub.

**Reading:** upstream deliberately dropped the ready-made layer to focus on the loop, and nobody in the community replaced it under a permissive license. It's exactly the gap this repository fills.

### 4.7 What can actually be reused

| Item | How to use it | Effort |
|---|---|---|
| **Self-hosted PR-Agent** | Covers the PR *review* axis, which we don't have today. Deploys as a second app in the same Fly org, with the same OpenRouter key | Low |
| **`.openhands_instructions` / `AGENTS.md` in the target repo** | Policy versioned **in the customer's repository** instead of hardcoded in `policy/label_router.py`. It's what's missing for the agent to stop being specific to one organization (§8.3) | Medium |
| **gh-aw patterns** | Safe-outputs, egress with an allowlist, triage report with confidence and justification (§5.2, §7.3) | Medium |
| **resolver / claude-code-action UX** | Label and mention as trigger — already mirrored in our `agent-fix-please`; missing the `@agente` mention | Low |
| **"Actions mode" offering** | Package the agent also as an Action for community adoption, keeping the service as the paid path | Medium |

### 4.8 The honest alternative: don't build

If the goal were merely *having our own product repository's issues handled*, the cheapest path today would be assigning issues to **Copilot coding agent** or committing a workflow with **claude-code-action** — and shutting down this service.

What's lost in that trade: verifiable cost control per task and per day, our own auditing, provider portability (today OpenRouter via LiteLLM), our own label policy, and the product thesis. What's gained: zero infra maintenance.

This report records the trade-off instead of hiding it. The decision belongs to the project owner — but it must be made explicitly, not by inertia.

### 4.9 Section conclusion

**Today there is no ready-made agent, under a permissive license, self-hostable on cheap infra, that does multi-repo issue→PR with declarative policy, cost control, and auditing.** The space is split into three blocks that don't fit: closed SaaS, Actions running on the customer's infra, and self-hosted options that are non-free (OpenHands Cloud) or licensed in production (open-swe).

This is simultaneously the project's **justification** and the **bad news**: there's no "just parameterize and deploy" shortcut. What's there to reuse is the review layer (PR-Agent), security patterns (gh-aw), and UX conventions — not the core.

---

## 5. Trigger model: App + webhook vs Actions

### 5.1 The two families

| Family | Examples | Where it runs | Who pays for compute | Fit |
|---|---|---|---|---|
| **GitHub App + webhook** (ours) | open-swe, Sweep, OpenHands Cloud | Our own infra | Us (or the SaaS) | Enables multi-repo/multi-tenant and execution outside the customer's runner |
| **GitHub Actions** | `anthropics/claude-code-action@v1`, `openhands-resolver`, `github/gh-aw` | Customer's runner | Customer | Trivial install, but ties to GitHub Actions and the customer's minutes budget |

The inventory of products behind each family is in §4; here the discussion is about which model to adopt.

### 5.2 What to learn from each one

**`openhands-resolver`** — a `fix-me` label on the issue triggers the Action, which spins up a sandboxed runtime, edits, runs tests, and opens a PR; it accepts `.openhands_instructions` in the target repo to customize the prompt. The code was absorbed into the OpenHands monorepo. **Code reuse: ~zero** (it's V0 legacy, the same reason that already made us revert in PRD iteration 3). **UX reuse: high** — the instructions file versioned in the target repo is what we're missing to become a generic product.

**`anthropics/claude-code-action@v1`** — `@claude` in a comment becomes a PR. It's the interaction pattern the market normalized. Our `agent-fix-please` fills an equivalent role via a label.

**`github/gh-aw` (GitHub Agentic Workflows, technical preview since Feb/2026)** — it's the **security** reference, not an engine reference. Workflows described in Markdown under `.github/workflows/`, compiled into Actions. The design:

- the agent's job is **read-only and sandboxed by default**;
- writes to GitHub go through separate **safe-outputs** jobs, with scoped permissions and validation;
- egress behind a proxy (squid) with an allowlist limited to `github.com` and approved defaults;
- the auto-triage workflow publishes a Discussion with a table of the issue, applied labels, **confidence level and justification** for each classification.

### 5.3 Recommendation

**Keep GitHub App + webhook.** It's what allows (a) running outside the customer's runner, (b) multi-repo with a single install, (c) charging per use — the three prerequisites of the SaaS path.

**Import two concepts from gh-aw:**

1. **Safe-outputs:** separate the process that executes LLM-generated code from the process that holds the write token. Today both live in the same container, with the installation token in the environment (§7).
2. **Auditable triage report:** we already comment on the issue; what's missing is exposing confidence and justification in a structured way and persisting the trajectory (§7.3).

---

## 6. Execution and sandbox on Fly.io

### 6.1 Current state, no euphemisms

**There is no sandbox.** The agent executes `subprocess.run(..., shell=True)` via `LocalEnvironment` (`runner/agent_factory.py:44`) **inside the same container** that runs the web API and the worker, as UID 1000, with `cwd` pointed at `/data/workspaces/<task-id>/<repo>/`.

- `cwd` is a convention, **not** confinement: nothing stops the agent from writing outside it.
- The GitHub App installation token is accessible to the process that executes the LLM's commands (`runner/workspace.py` builds the clone URL with the token).
- Real isolation today = the Fly Machine boundary (Firecracker). In other words: **the blast radius of a hostile command is the entire service**, including `/data` (the queue's SQLite database, other tasks' workspaces, audit logs).

This is a recorded decision ("scoped LocalWorkspace, not Docker sandbox", with Docker deferred to a later roadmap phase) and defensible while the only target repo is ours and the author list is an allowlist. **It stops being defensible the minute a third-party repo enters.**

### 6.2 Options

| # | Option | Isolation | Effort | Cost | Note |
|---|---|---|---|---|---|
| 1 | **mini-SWE-agent's `DockerEnvironment`** on the same Machine | Container inside the VM | Low — class swap in `agent_factory` | ~zero additional | Requires Docker-in-Docker or an exposed socket on the Machine; there's known friction on Fly. It's the "smallest leap" |
| 2 | **Ephemeral Fly Machine per task** (Machines API) | Full Firecracker VM, per task | Medium — worker becomes an orchestrator; needs workspace transport and result collection | Paid per second of execution | Boot <300ms, 35+ regions. **Naturally fits the queue model we already have**: `fetch_next()` is already the dispatch point |
| 3 | **Fly Sprites** (Jan/2026) | Persistent microVM, hibernates when idle | Medium | Granular billing per CPU-hour/GB-hour, with cold storage when idle | Better DX (100 GB NVMe, checkpoint/restore, lifecycle exposed via MCP since Mar/2026). Fly's thesis is "stop killing sandboxes". Third-party comparisons — **published by competitors** — point to high cost under high concurrency; verify with real load before adopting |
| 4 | **E2B / Daytona / Modal / Northflank** | VM/gVisor depending on the vendor | High (leaves Fly) | Variable | Only justified if per-sandbox cost dominates the bill. Adds a second infra vendor to the product |

### 6.3 Recommendation

- **Short term (our own repo):** option **1**. It's a class swap, closes the crudest part of the problem, and doesn't change the deploy topology.
- **Multi-tenant target:** option **2**. An ephemeral Machine per task gives per-tenant isolation *for free* in the architecture, resolves the single-machine concurrency limit, and aligns cost with usage — which is what a SaaS needs to bill.
- **Sprites** remains an alternative to reevaluate if boot/clone latency becomes a perceptible bottleneck; today it isn't.
- Keep the swap interface (`CodingAgentDelegator` + environment choice) as the single point where this decision lives.

### 6.4 Operational notes already paid for in production

Our internal deploy notes record the specific pitfalls of this setup, and they remain valid: the Docker image is a snapshot of the repo at deploy time; **Fly secrets don't sync with `.env`**; a 503 on `/readyz` makes the proxy block external traffic; the `LLM_MODEL` prefix must match the key's provider. Any sandbox change runs into these same points.

---

## 7. Security and prompt injection

### 7.1 The surface

The agent **reads untrusted input** (issue body, title, comments — anyone can open an issue on a public repo) and **holds write credentials** on the repository. This is precisely the combination exploited in the 2026 incidents.

### 7.2 What happened in 2026

| Incident | Date | Mechanics | Lesson |
|---|---|---|---|
| **Clinejection** (Cline) | Feb/2026 | A **malicious issue title** chained four vulnerabilities to compromise the project's npm package | No field of the issue is trusted data — not even the title |
| **Exfiltration via `claude-code-action`** | H1/2026 | Untrusted GitHub content processed by the agent led to CI/CD workflow secret exposure; Microsoft and CSA analyses described the chain. Mitigated on the Claude Code side (2.1.128, May/2026), hardening the Read tool | Vendor mitigation doesn't replace architectural isolation |

Consensus across the analyses: defense in three layers — **architectural prevention**, runtime detection, and governance. Prompt injection **is not a solved problem** for autonomous agents in high-risk contexts; the current recommendation is incremental rollout with human oversight at consequential points.

### 7.3 Checklist against our code

**We already have:**

- HMAC SHA-256 verification of the webhook signature (`webhook/validation.py`) and a requirement for `X-GitHub-Delivery`.
- Delivery deduplication before any work (`store.is_delivery_processed`).
- Repository and author allowlist (`policy/guards.py:31` and `:37`).
- Escape label `agent-skip` and a **never auto-merge** policy (AGENTS.md).
- Triple budget: `cost_limit`/`step_limit` on the agent (`runner/agent_factory.py:56-57`), wall-clock in the runner, daily cross-task ceiling (`policy/guards.py:56`).
- GitHub App with a short-lived installation token, not a PAT.
- Comments from the app itself are discarded (avoids a self-triggering loop).

**Missing:**

| Gap | Impact | Reference |
|---|---|---|
| **No execution isolation** | A hostile command reaches all of `/data`: queue, other tasks' workspaces, auditing | §6.1 |
| **Write token in the same process that executes the LLM's code** | Credential exfiltration in a single command | `runner/workspace.py` |
| **No egress restriction** | Exfiltration to any host; the gh-aw pattern is an allowlist with a proxy | §5.2 |
| **Trajectory not persisted** | `AgentConfig.output_path` is never set (`grep output_path src/` returns nothing), yet `docs/troubleshooting.md:215` instructs the operator to investigate `/data/events/<task-id>/trajectory.jsonl` — **a file that is never written**. Without auditing there's no way to investigate an incident | `docs/troubleshooting.md:215` |
| **System prompt without explicit boundaries** | Current recommendation: explicitly forbid sending file content to an external URL and reading/emitting files with `KEY`/`SECRET`/`TOKEN` | `prompts/system.md` |
| **Per-label prompts are dead in production** | `task.label_category` is never populated on the FIX path — `webhook/routes.py:123` doesn't set it and `worker.py:515` only computes the category inside the TRIAGE_ONLY branch. So `load_system_prompt(task.label_category)` (`runner/agent_factory.py:54`) always receives `None`, and `bug_fix.md`/`doc_fix.md` are never applied. `category_from_labels()` (`runner/prompts.py:43`) exists and does the right thing, but is never called on the execution path | cheap-to-fix bug, direct impact on agent quality and control |

### 7.4 Rule to adopt before any third-party repo

**No external repository is enabled while the process that executes LLM-generated code has access to the write token and unrestricted network.** Minimum order: sandbox (§6.3 option 1 or 2) → writes via a separate process (safe-outputs) → egress allowlist.

---

## 8. Licensing and SaaS viability

### 8.1 Candidate licenses

| Project | License | Redistribute? | Commercial use in SaaS? |
|---|---|---|---|
| mini-SWE-agent | MIT | Yes | Yes, no obligation beyond the notice |
| OpenHands SDK | MIT | Yes | Yes |
| SWE-agent | MIT | Yes | Yes (but dormant) |
| open-swe | MIT | Yes | Core yes; **production LangGraph Server requires a license** |
| Goose | Apache 2.0 (Linux Foundation / AAF) | Yes | Yes; foundation governance is a positive long-term point |
| Claude Agent SDK | Proprietary | No | Depends on Anthropic's terms and the account paying for tokens |
| PR-Agent (ready-made product) | MIT, community | Yes | Yes — but covers PR review, not issue→PR |
| OpenHands core (ready-made product) | MIT | Yes | Yes |
| **OpenHands Cloud self-hosted** | **Polyform Free Trial** | No | **No** — 30 days/year without a commercial license |

**Consequence:** keeping mini-SWE-agent (MIT) preserves full freedom both to open-source the code *and* to monetize. Adopting the Claude Agent SDK as the **sole** engine would create a proprietary dependency in the product's core — that's why it should enter as an *optional delegator* (`CodingAgentDelegator`), never as the mandatory loop.

### 8.2 Who already charges for this

| Product | Model |
|---|---|
| Sweep | Was the reference GitHub App issue → PR bot; **pivoted to a JetBrains plugin** (§4.3) |
| Codegen | Agent mention on Linear/Slack/GitHub → PR |
| Tembo | Layer on top of agents: chooses which harness runs (Claude Code, Codex, Cursor, OpenCode, Amp), with sandboxed VMs, Linear/Slack triggers, scheduled automations |
| OpenHands Cloud | Hosted version of the open source project |
| GitHub Copilot coding agent / Jules | From the platform owner itself. Assigning the issue and getting a PR back is now **native GitHub behavior** — it's the expectation floor any bot is measured against |
| Sentry Seer | Trigger is a production error, not an issue; billed per active contributor |

**Possible differentiation for this project:** it's not the engine (commodity) nor the integration (everyone has it). It's the combination of **per-repo declarative policy + verifiable cost control + complete auditing** — which is exactly the layer we've already built and that competitors treat as a detail.

### 8.3 What's missing in the code for multi-tenant

| Item | Situation today | Needed |
|---|---|---|
| Target repository | `settings.target_repo`, a single one, in an env var | Per App installation |
| Budget | `get_daily_budget` is **global** (one row per day) | Per installation/tenant, with its own ceiling and alert |
| LLM key | One global `LLM_API_KEY` | **BYOK per installation** (and the provider/prefix matching warning in `config.py` becomes per-tenant) |
| Label policy | Hardcoded in `policy/label_router.py` | File versioned in the target repo (`.openhands_instructions` / `AGENTS.md` pattern) |
| Names | Hardcoded bot and Fly app names, `.env` with `TARGET_REPO` | Parameterize bot and app identity |
| Isolation | One shared container | §6.3 option 2 |
| Persistence | SQLite on a single Machine's volume | Postgres above ~500 tasks/day (already on the roadmap) |

### 8.4 Documentation discrepancies to fix

Found during the audit — **all resolved since**, kept here as a record:

1. ~~`README.md:9` and `AGENTS.md` still advertise "OpenHands V1 SDK" and "OpenHands service on Fly.io"~~ — fixed to `mini-SWE-agent 2.4.6` (the real engine at that point).
2. ~~An earlier label-alignment document (§2.1) decides that **severity is parsed from the issue body, without `severity:*` labels**; `policy/label_router.py:21-23` still only reads the `severity:high`/`severity:critical` labels~~ — the file was renamed to `docs/label-schema.md` and rewritten to describe the actual behavior (labels win; see `docs/label-schema.md` §3).
3. ~~`docs/troubleshooting.md:215` promises a `trajectory.jsonl` that the code never writes~~ — the code actually writes `/data/events/<task-id>/trajectory.json` (Phase 2); the runbook now references the correct path and format.

---

## 9. Recommendation and backlog

### 9.1 Verdict

**Keep mini-SWE-agent as the engine.** The research found no engine that justifies migration:

- OpenHands matured, but remains a fast-moving target (22 minors in 3 months) and **doesn't buy measurable performance** against mini with the same model.
- SWE-agent is dormant.
- Claude Agent SDK is better per task, but proprietary — it fits as a delegator, not as the core.
- open-swe is a good architecture reference and a poor dependency choice.

**And it found no ready-made agent that replaces the service.** On the installable-products side (§4): what's free doesn't operate multi-repo in production without work (OpenHands core, resolver in Action); what operates isn't free (OpenHands Cloud under Polyform + Kubernetes) or requires a production license (open-swe/LangGraph Server); and the only trivially self-hostable one — PR-Agent, MIT — **doesn't do issue→PR**, only review. mini-SWE-agent itself had an issue runner in v1 and removed it in v2 (§4.6).

What the research found was something else: **the available gains are in updating and hardening what already exists**, not in switching foundations or adopting a ready-made product. And it confirmed the project's central thesis — the engine became a commodity, the value is in orchestration, and `CodingAgentDelegator` is the right bet.

Caveat recorded in §4.8: if the goal were merely having the target repo's issues handled, assigning them to Copilot coding agent or committing a workflow with `claude-code-action` is cheaper than maintaining this service. Keeping the project is a product decision, and must be made as one.

### 9.2 Backlog ordered by value/effort

All items below are **out of scope for this document** — they become an issue or a roadmap phase.

| # | Action | Value | Effort | Reason |
|---|---|---|---|---|
| 1 | Bump the lock to **2.4.6** and make `MiniSWERunner` consume the return value of `agent.run()` (`finish_reason`/`exit_status`) | High | Low | Closes the budget-overrun-reported-as-success bug and the abandoned-thread-on-timeout bug (§2.1) |
| 2 | Populate `task.label_category` on the FIX path by calling `category_from_labels()` | High | Low | Today `bug_fix.md`/`doc_fix.md` are dead prompts (§7.3) |
| 3 | Set `AgentConfig.output_path` and persist the trajectory to `/data/events/<task-id>/` | High | Low | Prerequisite for auditing, incident investigation, and any sale (§7.3) |
| 4 | Fix `README.md` and `AGENTS.md` (stack says OpenHands) | Medium | Trivial | Credibility when opening the repo (§8.4) |
| 5 | Resolve the severity contradiction between `label_router.py`, the label-alignment doc, and the roadmap | Medium | Low | Undefined router behavior (§8.4) |
| 6 | Sandbox: `DockerEnvironment` on the current Machine | High | Medium | Closes `LocalEnvironment`'s blast radius (§6.3) |
| 7 | Explicit security boundaries in `prompts/system.md` + egress allowlist | High | Medium | Injection mitigation recommended by the 2026 analyses (§7.3) |
| 8 | Separate the write token from the process that executes the LLM's code (safe-outputs pattern) | High | High | Condition for enabling a third-party repo (§7.4) |
| 9 | Implement a real `CodingAgentDelegator` (Claude Agent SDK) | Medium | Medium | Validates the pluggable-engine thesis |
| 10 | Ephemeral Fly Machine per task | Medium | High | Multi-tenant target; aligns cost with usage (§6.3) |
| 11 | Budget and LLM key per installation (BYOK) | Medium | High | SaaS prerequisite (§8.3) |
| 12 | Deploy self-hosted **PR-Agent** for the PR review axis | Medium | Low | Covers a gap without writing code; MIT, container, same OpenRouter key (§4.7) |
| 13 | Move the label policy to a file versioned in the target repo (`.openhands_instructions`/`AGENTS.md` pattern) | Medium | Medium | Condition for the agent to stop being specific to one organization (§4.7, §8.3) |

**Priority note:** items 1-5 add up to little effort and close defects that currently affect production. Production validation runs with more confidence after them than before.

---

## 10. Sources

Consulted on **2026-09-03**.

### Primary

- mini-SWE-agent — releases: https://github.com/SWE-agent/mini-swe-agent/releases
- mini-SWE-agent — PyPI: https://pypi.org/project/mini-swe-agent/
- mini-SWE-agent — docs: https://mini-swe-agent.com/latest/
- OpenHands Software Agent SDK — releases: https://github.com/OpenHands/software-agent-sdk/releases
- OpenHands SDK — docs: https://docs.openhands.dev/sdk
- OpenHands SDK — paper (arXiv 2511.03690): https://arxiv.org/abs/2511.03690
- SWE-agent — releases: https://github.com/SWE-agent/SWE-agent/releases
- open-swe (LangChain): https://github.com/langchain-ai/open-swe
- openhands-resolver: https://github.com/All-Hands-AI/openhands-resolver
- Claude Code — GitHub Actions: https://code.claude.com/docs/en/github-actions
- Claude Agent SDK — hosting: https://code.claude.com/docs/en/agent-sdk/hosting
- GitHub Agentic Workflows (gh-aw): https://github.com/github/gh-aw — technical preview changelog: https://github.blog/changelog/2026-02-13-github-agentic-workflows-are-now-in-technical-preview/ — triage workflow: https://github.github.com/gh-aw/blog/2026-01-13-meet-the-workflows/
- Fly.io — AI/agents: https://fly.io/ai/ — per-user environments with Machines: https://fly.io/docs/blueprints/per-user-dev-environments/
- PR-Agent (MIT, community): https://github.com/The-PR-Agent/pr-agent — PyPI: https://pypi.org/project/pr-agent/
- OpenHands-Cloud (self-hosted, Polyform Free Trial, Helm/k8s): https://github.com/OpenHands/openhands-cloud
- OpenHands — GitHub integration (`openhands` label / `@openhands`): https://docs.openhands.dev/openhands/usage/cloud/github-installation
- mini-SWE-agent — v1→v2 migration guide (issue runner removal): https://mini-swe-agent.com/latest/advanced/v2_migration/ — `mini-extra`: https://mini-swe-agent.com/latest/reference/run/mini_extra/
- `openai/codex-action`: https://github.com/openai/codex-action
- Sweep — repository with notice of pivot to a JetBrains plugin: https://github.com/sweepai/sweep
- Sentry Seer / Autofix: https://docs.sentry.io/product/ai-in-sentry/seer/autofix

### Secondary (security)

- Microsoft Security — "Securing CI/CD in an agentic world: Claude Code GitHub action case" (Jun/2026): https://www.microsoft.com/en-us/security/blog/2026/06/05/securing-ci-cd-in-agentic-world-claude-code-github-action-case/
- Cloud Security Alliance — research note on prompt injection in claude-code-action: https://labs.cloudsecurityalliance.org/research/csa-research-note-claude-code-github-action-prompt-injection/
- Aikido — "PromptPwnd: prompt injection inside GitHub Actions": https://www.aikido.dev/blog/promptpwnd-github-actions-ai-agents

### Tertiary (leads, not evidence)

- Simon Willison on the Sprites.dev launch (Jan/2026): https://simonwillison.net/2026/Jan/9/sprites-dev/
- "Sprites Speak MCP" (Aug/2026): https://bex.co/blog/2026/08/06/sprites-speak-mcp-agent-sandbox-tool
- Northflank — sandbox pricing comparison (**published by a competitor**): https://northflank.com/blog/ai-sandbox-pricing
- Blaxel — alternatives to Fly Sprites (**published by a competitor**): https://blaxel.ai/blog/fly-io-sprites-alternatives-ai-agent-sandboxes
- Tembo — alternatives to Codegen (**published by a competitor**): https://www.tembo.io/blog/codegen-alternatives
- OpenHands — "Top 7 self-hosted and open-source AI coding agents" (**published by an interested party**): https://www.openhands.dev/blog/open-source-ai-coding-agents

### Internal

- Internal design, engine-study and deploy notes (not published)
- `docs/troubleshooting.md`, `docs/label-schema.md`, `AGENTS.md`
