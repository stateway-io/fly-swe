# Research: Entire and how it fits fly-swe

> **Non-normative research snapshot.** This document records an evaluation of
> [Entire](https://entire.com) as of the date below; it is not kept in sync
> with Entire or with the codebase. For current, authoritative behaviour see
> [`docs/operations.md`](../operations.md), [`docs/providers.md`](../providers.md)
> and the source under `src/`.

**Version:** 1.0
**Date:** 2026-10-29
**Status:** Snapshot — recommendation, not a commitment
**Verified against:** docs.entire.com (Entire CLI 0.10.2), the Entire API OpenAPI spec, and `entireio/external-agents`
**Complements:** [`agent-landscape.md`](./agent-landscape.md), [`mini-swe-agent-api.md`](./mini-swe-agent-api.md)

---

## TL;DR

| Question | Short answer |
|---|---|
| Does Entire make sense for fly-swe? | **Yes, but only part of it.** Its session capture (*checkpoints*) adds something fly-swe lacks: a durable, reviewable record of *why* the agent wrote each change, stored with the code. The forge part (hosting, Trails) doesn't fit yet. |
| Provider or another kind of plugin? | **Another kind of plugin.** Session capture works on top of any Git forge, GitHub included, and belongs to the runner, near `runner/toolchains/`, not in `providers/`. Entire *could* also become a `ProviderPlugin` for repositories hosted natively on Entire, but its model maps poorly onto ours today, so that waits. |
| Is mini-swe-agent compatible? | **Not out of the box, but it can be made to work.** Entire does not support mini-swe-agent natively, and mini-swe-agent has no lifecycle hooks. fly-swe runs the agent loop itself, though, so the harness can send the lifecycle events Entire expects, the same way Entire's Python adapter does for LangGraph and CrewAI. |

---

## Table of Contents

1. [Context and method](#1-context-and-method)
2. [What Entire is](#2-what-entire-is)
3. [The problem it solves](#3-the-problem-it-solves)
4. [How it works](#4-how-it-works)
5. [Does it make sense for fly-swe?](#5-does-it-make-sense-for-fly-swe)
6. [Provider or another kind of plugin?](#6-provider-or-another-kind-of-plugin)
7. [Is mini-swe-agent compatible?](#7-is-mini-swe-agent-compatible)
8. [Risks and open questions](#8-risks-and-open-questions)
9. [Recommendation and backlog](#9-recommendation-and-backlog)
10. [Sources](#10-sources)

---

## 1. Context and method

fly-swe takes a labelled issue and produces a PR that a human reviews. The
reviewer sees the diff, the PR body (`core/pr_text.py`) and any progress
comments. The agent's full trajectory is written to
`<data_dir>/events/<task-id>/trajectory.json` on the Fly volume
(`runner/agent_factory.py`), where only an operator with machine access can read it.

Entire is a new forge from the former GitHub leadership. It is built around
the claim that agent-written code needs its context kept alongside it. This
document asks whether that idea, or the product, belongs in fly-swe.

Method: I read the public docs at docs.entire.com: concepts, checkpoints,
storage, security, Trails, CI, migration, the CLI reference and the full
Agent Integration Protocol. I also read the published OpenAPI spec
(`https://aws-us-east-2.api.entire.io/api/v1/openapi.json`) and the
`entireio/external-agents` repository. I did not run the Entire CLI
against a real repository; [§9](#9-recommendation-and-backlog) proposes a
spike to do that.

## 2. What Entire is

Entire calls itself *a Git forge built for collaboration between people and
AI coding agents*. It has two layers, and you can adopt them separately.

### 2.1 Entire CLI: session capture

- An open-source CLI (MIT) that you enable in any Git repository (`entire enable`).
- It installs **agent hooks**, which record what the agent did, and **Git
  hooks**, which attach that record to commits and pushes.
- Each recorded unit of agent work is a **session**. When a commit is made
  during a session, Entire creates a **checkpoint** that pairs the commit with
  the prompts, transcript, tool calls, files touched and token usage behind it.
- Checkpoints live **in your own repository**, under
  `refs/entire/checkpoints/<shard>/<id>`, and are pushed to your normal remote
  (GitHub included) on `git push`. Entire does not need to be your forge, and
  capture does not need an entire.com login.
- Agents supported natively: Claude Code (the default), Codex, Cursor,
  Copilot CLI, Antigravity CLI, Factory Droid, OpenCode and Pi. Others are
  added through the **Agent Integration Protocol** ([§4.4](#44-agent-integration-protocol)).
- On top of the stored history: `entire why` / `blame` / `investigate` (why
  does this code exist?), `session resume` (pick up an agent session from
  any checkpoint), search, recaps and *dispatches* (summaries of agent work).

### 2.2 entire.com: the forge

- **Hosting**: repositories hosted natively on Entire's Git network
  (`entire://<cluster>/et/<project>/<repo>`), with regional placements in the
  US, EU, UK, India, Australia and Brazil. Or a **mirror** of a GitHub
  repository, where GitHub stays the source of truth and pushes are
  forwarded to it. Branches prefixed `entire/unmirrored/` stay on Entire
  only, so work can continue during a GitHub outage.
- **Trails**, which Entire describes as *"our replacement for the pull
  request for agentic coding"*. A trail can start as a bug report, feature
  request or task, with title, body, type, priority and assignees. It can
  group branches (*changes*) across several repositories, and it carries
  discussions, approvals, the agent sessions, and merge requirements
  (**Gates**: branch up to date, human review, agent findings, CI checks).
- **Review agents / runners**: configurable AI jobs that run in Entire's
  cloud sandboxes on every push to a trail and post scores and *findings*
  (code review, security, "slop", drift, risk…).
- **CI plugins** (public preview): Buildkite and Depot report checks back to trails.
- **Brain / entire-graph / search**: shared project memory, a semantic code
  graph, and search across code, commits and sessions, all meant for agents to read.

## 3. The problem it solves

> A diff shows what changed but not why.

When an agent writes a change, the diff loses the reasoning behind it: the
original request, the files the agent read, the commands it ran and what
they printed, the dead ends it tried, and the trade-offs it chose. Entire
keeps that context **next to the Git history it explains**, so that:

- a **reviewer** can check how the agent got to a change, not only the result;
- a later **maintainer** can trace a line of code back to the prompt that produced it;
- **work can be handed over**: another person or agent can resume from the exact
  session context instead of starting from scratch;
- **teams** can search and reuse past agent work as shared memory.

The forge layer adds a second argument: agents pull and push far more often
than humans do, so hosting close to where agents run, and staying available
when GitHub is down, matters more than it used to.

## 4. How it works

### 4.1 Checkpoint capture

1. Agent hooks fire during the session (session start, prompt submitted,
   turn finished, compaction, session end). They capture the transcript and
   the files touched. While a session is live, working state goes to local
   **shadow branches** (`entire/<commit>-<worktree>`).
2. On `git commit`, a Git hook links recent agent work to the commit and adds
   a trailer (`Entire-Checkpoint: <ULID>`). An agent's own commits are linked
   the same way. `commit_linking: "always"` skips the interactive prompt.
3. Metadata and transcripts are **redacted** (pattern- and entropy-based secret
   scanning) before every write, then stored as one ref per checkpoint.
4. On `git push`, a pre-push hook pushes the queued checkpoint refs to one
   elected *checkpoint remote*. You can redirect them to a **separate
   repository** (`--checkpoint-remote github:org/checkpoints-private`) or keep
   them local (`push_sessions: false`).

A checkpoint stores `prompt.txt`, `full.jsonl`, `transcript.jsonl`,
`metadata.json`, `checkpoint.json` and optional `assets/`. It records the
paths of touched files, not their contents. **Anyone with read access to the
repository that holds the checkpoints can read them.** Entire's own security
page says redaction is "a safety net rather than a guarantee".

### 4.2 Trails as a PR replacement

A trail combines **intent** (similar to an issue) and **change review**
(similar to a PR) in a single object:

| GitHub concept | Entire equivalent |
|---|---|
| Issue | Trail with no linked branch (the *Planning* phase), `type: bug \| feature \| task` |
| Pull request | A *change* (a linked branch) inside a trail; one trail can hold several changes across repositories |
| Issue / PR comments | Trail *discussions* (threads with messages) and review comments |
| Labels | **None.** A trail has `type`, `priority`, `assignees` and free-form `metadata` |
| Required reviews / status checks | Gates (human review, agent review, up to date, checks) |
| Merge | `POST …/trails/{number}/merge` once the gates pass |

On **GitHub mirrors**, a change is still backed by a GitHub PR for CI and
merging, and *"Entire reviews and GitHub reviews are separate"*.

### 4.3 API and webhooks

- An OpenAPI 3 spec covers trails, changes, discussions, reviews, approvals,
  gates, merge, repositories and sessions. It authenticates with an OAuth2/OIDC
  bearer JWT, and `ENTIRE_TOKEN` provides a token for CI and automation.
- The CLI's `entire api` command warns that these endpoints are *"internal
  … may change without notice"*. The spec itself is version `0.1.0`.
- **Plugin webhooks** exist: `installation.*`, `ref.created|updated|deleted`
  and `trail.opened|updated|reopened|closed|merged`. They are signed as an
  EdDSA JWT verified through the delivering cell's JWKS, with a `body_sha256`
  claim, a 5-minute lifetime and a `delivery_id` for deduplication. The docs
  only describe them in the context of the **CI plugin** program (Buildkite,
  Depot; public preview). There is **no event for new discussion
  messages**, which fly-swe would need for comment follow-ups.

### 4.4 Agent Integration Protocol

To add an agent Entire does not ship with:

- Provide an executable named `entire-agent-<name>` on `$PATH`. The CLI
  discovers it at startup and calls it with **stateless subcommands that
  exchange JSON over stdin/stdout**, receiving `ENTIRE_REPO_ROOT` and
  `ENTIRE_PROTOCOL_VERSION=1`.
- Required subcommands: `info`, `detect`, `get-session-id`, `get-session-dir`,
  `resolve-session-file`, `read-session`, `write-session`, `read-transcript`,
  `chunk-transcript`, `reassemble-transcript`, `format-resume-command`.
- Optional capabilities: `hooks` (`parse-hook`, `install-hooks`, …),
  `transcript_analyzer` (modified files, prompts, summary),
  `transcript_preparer`, `token_calculator`, `text_generator`,
  `hook_response_writer`, `subagent_aware_extractor`.
- Lifecycle events: `SessionStart`, `TurnStart` (the CLI snapshots git
  state), `TurnEnd` (the CLI reads and chunks the transcript and stores a
  checkpoint), `Compaction`, `SessionEnd`, `SubagentStart/End`. Each
  subcommand has a 30 s timeout and a 10 MB output cap, with no retries.
- **Python precedent:** the `entire-adapter` package (PyPI) adds LangGraph
  (`EntireCallbackHandler`) and CrewAI (`EntireCrewAIListener`). These
  frameworks have no hook files of their own, so the adapter fires the
  lifecycle hooks **from in-process callbacks** through
  `entire hooks <agent> <hook>`, and writes a JSONL transcript outside the
  worktree that it passes as `session_ref`. `install-hooks` just writes a
  marker under `.entire/`. This is the pattern fly-swe would follow
  ([§7](#7-is-mini-swe-agent-compatible)).

## 5. Does it make sense for fly-swe?

**Yes, for session capture. Not yet for the forge.**

### 5.1 Where it adds value

- **Review is the product's safety mechanism.** fly-swe never merges, and a
  human always reviews. Today that reviewer gets the diff and a summary,
  while the trajectory that would let them judge the change sits on the Fly
  volume where they can't reach it. Checkpoints attach it to the PR's
  commits, in the repository the reviewer already has access to.
- **Provenance outlives the task.** Workspaces under
  `/data/workspaces/<task-id>/` and events under `/data/events/` are
  operational data with a retention policy (`observability/cleanup`).
  Checkpoints stay in the target repository's history, so months later
  `entire why <file>:<line>` still finds the issue and the reasoning behind a line.
- **Follow-ups and retries.** fly-swe already brings back *previous work*
  when an agent revisits a PR (`<previous_work>` in the instance template).
  Checkpoints are a more complete, standard store for the same thing, and a
  human could also resume one locally with their own agent.
- **Low coupling.** Capture is plain Git (refs, a trailer, hooks). It needs no
  entire.com account and does not change the provider model, the queue or
  the GitHub App.

### 5.2 Where it doesn't fit (yet)

- **The forge layer is young and its interfaces are unstable**: the API is
  version 0.1.0 and documented as internal, and the webhook program is
  scoped to CI plugins in public preview.
- **No labels.** fly-swe's routing is label-driven (`policy/` label router,
  [`label-schema.md`](../label-schema.md)). Trails only offer `type`/`priority`/`metadata`.
- **One object for issue and PR.** fly-swe's canonical model keeps the issue
  (`IssueRef`) apart from the PR it produces (`PullRequestClient`). A trail
  holds both.
- **Overlap with fly-swe's own job.** Entire's review agents and Trails
  automate the review loop with agents running in Entire's sandboxes. fly-swe
  could run *alongside* them (fly-swe writes the change, Entire's runners
  review it), but that is a product decision and not needed for capture.

## 6. Provider or another kind of plugin?

Entire offers two separate things, and they belong in two different places.

### 6.1 Session capture: a new plugin kind, not a provider

A `ProviderPlugin` (`providers/base.py`) is a **forge**: it receives webhooks
(`WebhookAdapter`), reads and writes issues and comments (`ForgeClient`),
opens PRs (`PullRequestClient`) and supplies clone/push credentials
(`GitTransport`). Session capture does none of that. It:

- runs **inside the runner**, around the agent loop and the harness's
  `git commit` (`runner/executor.py`, the commit step near line 2574);
- works with **any** forge: the checkpoint refs go wherever the code is pushed;
- is selected per repository, like a toolchain.

So it fits next to `runner/toolchains/` (a port, a registry and per-repo
config) rather than next to `providers/`. A sketch, not a design:

```python
class SessionRecorder(Protocol):
    """Records an agent run so it can be linked to the harness's commit."""
    name: str
    def prepare(self, workspace: Path, task: Task) -> None: ...        # entire enable --local --yes …
    def on_session_start(self, prompt: str) -> None: ...
    def on_step(self, step: int, messages: list[dict], cost: float) -> None: ...
    def on_session_end(self, outcome: str) -> None: ...
    # The commit itself stays with the harness; Entire's git hook links it.
```

Config would sit beside `repos[].project.toolchain`, for example
`repos[].project.session_recorder: none | entire` plus a global
`session_recorders.entire:` block (checkpoint remote, `push_sessions`,
binary path). It would follow the forge-neutral rule: the recorder never
names a forge, and the provider's `GitTransport` keeps owning credentials.

### 6.2 Entire as a forge: a possible future `ProviderPlugin`

For repositories hosted **natively** on Entire (where GitHub is absent), Entire
*would* be a provider under `providers/entire/`, and the mapping is
possible on paper:

| Port | Entire mapping | Fit |
|---|---|---|
| `WebhookAdapter` | Plugin webhooks, `trail.opened` with `type: bug\|feature` → `TriggerEvent`; JWT/JWKS verification; `delivery_id` as the delivery key | Partial: no discussion-message event, so no comment-driven follow-ups |
| `ForgeClient` | Trail = issue; discussions = comments; `get_default_branch` from the repository | Partial: `add_labels` has no target (`supports_labels=False`), `author_permission` → project roles |
| `PullRequestClient` | Create a *change* on the trail (`POST /{host}/{project}/trails/{id}/changes`), mergeability endpoint | Awkward: the "PR" lives inside the "issue" |
| `GitTransport` | `entire://` remotes via `git-remote-entire`, bearer token from `ENTIRE_TOKEN` | Needs an extra Git remote helper in the image |
| `capabilities.terms` | issue → "trail", PR → "change" | Fine: the terms system was built for this |

**Recommendation:** don't build it now. Revisit if a target repository
actually moves to native Entire hosting **and** Entire publishes a stable
public API with discussion webhooks. For **GitHub mirrors**, the GitHub
provider stays as it is: Entire mirrors the repository, and checkpoints
travel with the pushes fly-swe already makes.

## 7. Is mini-swe-agent compatible?

**Not natively. It can be made compatible, at a moderate cost.**

- mini-swe-agent is not one of Entire's native agents and is not in
  `entireio/external-agents`, which ships Amp, Goose, Kiro, LangGraph,
  CrewAI, Qwen, Grok, Hermes, Kilo and OMP.
- Entire's native integrations depend on the agent's own lifecycle hooks.
  mini-swe-agent has none: it is a small Python loop with a `bash`-only action space.
- **But fly-swe owns the loop.** `runner/agent_factory.py` builds the agent,
  already wraps it with an `on_progress` callback, and writes the trajectory.
  That is the same situation as LangGraph/CrewAI, which Entire supports
  through in-process callbacks.

What it would take:

1. **A transcript writer**: the harness appends each mini-swe-agent message
   (system/user/assistant/observation, with cost) to a JSONL file outside the
   worktree. This is a by-product of the existing trajectory, not a new data source.
2. **An `entire-agent-mini-swe-agent` executable**, written in Python and
   shipped with this package (a console script) or vendored from
   `entire-adapter`. It implements the required subcommands over that JSONL,
   and optionally `transcript_analyzer` (modified files from `git status` or
   the observations) and `token_calculator` (from the LiteLLM costs we already track).
3. **Workspace setup**: in each task workspace, after cloning, run
   `entire enable --agent mini-swe-agent --local --yes --telemetry=false`.
   The settings go in `.entire/settings.local.json` (gitignored), so nothing
   lands in the target repository's tree.
4. **Fire the lifecycle** from the harness: SessionStart before
   `agent.run()`, TurnEnd per step (or every N steps), SessionEnd after.
   The harness's own `git commit` then gets the `Entire-Checkpoint` trailer
   from Entire's Git hook.

This respects the [Agent Execution Contract](../operations.md#the-agent-execution-contract):
the coding agent still only edits files. The hooks run in the harness
process, not in the agent's minimal-env subprocess, and Entire's Git hook
runs on the commit the harness already makes.

**A lighter variant** is to skip live hooks: at the end of the run, convert
the trajectory to a transcript and call `entire session attach` (link a
session to the latest commit) or `entire import`. It needs the same
executable and transcript format, but no per-step calls. This variant is
less tested upstream, so the spike should try it first.

**Alternative**: Claude Code and Codex are supported natively. If fly-swe
ever delegates to one of those engines (an earlier, unimplemented
delegator protocol was removed in v1.1.10), capture would come almost for
free with that engine.
Changing the engine just to get capture is not justified.

## 8. Risks and open questions

| Risk | Note / mitigation |
|---|---|
| **Untrusted text in published transcripts** | Transcripts contain the issue body and command outputs. With checkpoints pushed to the target repository, everyone with read access sees them, including the public on a public repo. Mitigation: a **separate private checkpoint repository** (`--checkpoint-remote`), or `push_sessions: false` for public repositories. |
| **Secret leakage** | The agent subprocess already runs with a minimal env and no secrets (`runner/environment.py`). Entire's redaction is an extra layer, not a guarantee. Keep the token out of transcripts: the clone URL with credentials must never appear in a captured command output. |
| **Shadow branches hold raw working-tree snapshots** | They stay local under `/data/workspaces/<task>/` and are not pushed. Workspace cleanup already removes them. |
| **Pushing `refs/entire/*` with the GitHub App token** | Pushing non-branch refs needs `contents: write`, which the App already has. Check that branch protection and rulesets don't reject these refs. A separate checkpoint repository needs its own credentials. |
| **Another binary in the image** | The Entire CLI (Go) and, for native hosting, `git-remote-entire`. It could be provisioned like a toolchain into `/data/tools` instead of baking it into the image. |
| **Hook cost** | Each hook runs a subprocess with a 30 s timeout and no retries. Firing per step adds latency, so batch or fire every N steps. |
| **Vendor maturity** | The CLI moves fast (ref-based storage became the default in 0.10.0; earlier repositories use a branch). Pin a version, as fly-swe already does for mini-swe-agent. |
| **Telemetry** | The CLI sends anonymous PostHog analytics unless disabled. Use `--telemetry=false`. |
| Open: does `session attach` / `import` work with external agents? | The docs only show native agents. Settle this in the spike. |
| Open: `entire.com` value without native hosting | Checkpoints are readable with plain Git, but browsing them conveniently needs the Entire GitHub App on the target repository, which is a decision for each repository owner. |

## 9. Recommendation and backlog

1. **Spike (time-boxed):** by hand, in one workspace of a private target
   repository, write the `entire-agent-mini-swe-agent` executable and a
   JSONL transcript from a real trajectory, try `session attach` and
   live hooks, push checkpoints to a private checkpoint repository, and
   confirm a reviewer can read them (`entire checkpoint explain`, entire.com).
2. **If the spike holds:** add a `SessionRecorder` port in `runner/` with a
   `none` default and an `entire` implementation, per-repository config,
   provisioning like a toolchain, unit tests with a fake recorder, and an
   opt-in e2e test. Document it in `operations.md`, and put a link to the
   checkpoint in the PR body (`core/pr_text.py`).
3. **Deferred:** an `entire` `ProviderPlugin` for native Entire hosting.
   Revisit when Entire's public API and discussion webhooks are stable and a
   real repository needs it.
4. **Not recommended:** replacing fly-swe's review flow with Trails gates,
   or switching engines only to get native capture.

## 10. Sources

- Entire docs — introduction and index: <https://docs.entire.com/>, <https://docs.entire.com/llms.txt>
- Checkpoints: <https://docs.entire.com/guides/checkpoints/capture-checkpoints.md>, <https://docs.entire.com/guides/checkpoints/checkpoint-storage.md>, <https://docs.entire.com/guides/checkpoints/checkpoint-destination.md>, <https://docs.entire.com/guides/checkpoints/keep-checkpoints-local.md>
- Git hooks: <https://docs.entire.com/guides/configuration/git-hooks.md>
- Security and privacy: <https://docs.entire.com/security.md>
- Repositories and mirrors: <https://docs.entire.com/guides/repositories/repository.md>, <https://docs.entire.com/guides/repositories/mirrors.md>
- Trails, agents, gates: <https://docs.entire.com/guides/trails/overview.md>, <https://docs.entire.com/guides/trails/agents.md>, <https://docs.entire.com/guides/trails/gates.md>
- CI plugins: <https://docs.entire.com/guides/ci/overview.md>
- Agent integrations: <https://docs.entire.com/agents/overview.md>
- Agent Integration Protocol: <https://docs.entire.com/agents/agent-integration-protocol/overview.md> (architecture, commands, lifecycle, data model, build guide)
- Import past sessions: <https://docs.entire.com/guides/sessions/import-past-agent-history.md>
- Migrate from GitHub: <https://docs.entire.com/migration/migrate-from-github.md>
- CLI reference (`api`, `auth`, `runner`): <https://docs.entire.com/cli-reference/api.md>, <https://docs.entire.com/cli-reference/auth.md>, <https://docs.entire.com/cli-reference/runner.md>
- Entire API OpenAPI spec: <https://aws-us-east-2.api.entire.io/api/v1/openapi.json>
- External agents (incl. LangGraph/CrewAI `entire-adapter`): <https://github.com/entireio/external-agents>
