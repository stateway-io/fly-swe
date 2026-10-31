# Research: coding benchmarks for picking `LLM_MODEL`

> **Non-normative research snapshot.** This document records the state of the
> public coding-agent leaderboards as of the date below; it is not kept in sync
> with upstream. Re-check the sources before relying on any number.

**Version:** 1.0
**Date:** 2026-10-18
**Status:** Historical snapshot
**Complements:** [`docs/research/agent-landscape.md`](./agent-landscape.md) §3.3

---

## Table of Contents

1. [Why this survey](#1-why-this-survey)
2. [The swebench.com leaderboard has stopped updating](#2-the-swebenchcom-leaderboard-has-stopped-updating)
3. [Selection criterion: issue → PR](#3-selection-criterion-issue--pr)
4. [Candidates](#4-candidates)
5. [Recommendation](#5-recommendation)
6. [Sources](#6-sources)

---

## 1. Why this survey

[`agent-landscape.md`](./agent-landscape.md) §3.3 concludes that our lever is
`LLM_MODEL`, not the engine, and backs it with SWE-bench Verified numbers. When
we went back to [swebench.com](https://www.swebench.com/) to pick a model, the
leaderboard had not moved in months. This document records why, and which
public benchmark should replace it as our reference.

## 2. The swebench.com leaderboard has stopped updating

### 2.1 Evidence

The site is rendered from `data/leaderboards.json` in
[`SWE-bench/swe-bench.github.io`](https://github.com/SWE-bench/swe-bench.github.io).
Most recent entry date per board:

| Board | Latest entry | Entries |
|---|---|---|
| Verified | 2026-02-26 | 180 |
| Multilingual | 2026-02-20 | 13 |
| Test (full) | 2025-12-19 | 24 |
| Multimodal | 2025-11-17 | 22 |
| Lite | 2025-09-11 | 84 |

- The file was last changed on 2026-09-01 (`193160a`, "Drop bash-only board").
- [`SWE-bench/experiments`](https://github.com/SWE-bench/experiments), where submissions
  land, merged two entries after that, on
  2026-09-01 and 2026-09-02: `verified/20260901_mini-v2.4.2_gemini-3-5-flash`
  (experiments#475) and `multilingual/20260902_mini-v2.4.6_gemini-3-5-flash` (experiments#476).
  **Neither is on the website.**
- The `experiments` CI (`.github/workflows/validate.yml`) checks that
  `analysis.get_leaderboard` compiles but publishes nothing. Moving results to the
  site is a manual step, and nobody has done it since September.
- 22 submission PRs opened in `experiments` since 2026-09-02 are still open, none of them
  merged. Several make implausible claims (96–100%). Community PRs on the site repo have
  waited since February 2026 (#51, #52, #56–#58, #60, #61).
- No notice on the site and no upstream issue explains the pause (checked both repos on
  2026-10-18). We asked upstream in [SWE-bench#691](https://github.com/SWE-bench/SWE-bench/issues/691).

### 2.2 Likely causes

These are documented elsewhere, but not on the leaderboard itself.

1. **Submission policy (2025-11-18).** The `experiments` README restricts Verified and
   Multilingual to academic or research-lab teams with open methods and a preprint.
   Product submissions (e.g. Augment Code, Solver AI, Honeycomb.sh) are no longer eligible.
   This cut off most incoming entries. Multimodal stays open to everyone.
2. **Saturation and contamination.**
   - A SWE-bench co-creator wrote on Hacker News that Verified is "saturated at 93.9%".
   - OpenAI stopped reporting Verified in February 2026 and now recommends SWE-bench Pro,
     citing flawed tests and contamination.
   - Vals AI no longer runs Verified on new models. Its last listing (2026-09-01) shows
     Claude Opus 5 at 97.00%.
   - Community analyses filed upstream make the same point. SWE-bench#621: the top 8
     submissions are one statistical tie. SWE-bench#650: 54% of instances don't
     discriminate among the top 25.
3. **The team moved on.** Upstream activity since August 2026 is a new self-hosted
   submission pipeline: `swebench submit package|publish|register`, plus Hugging Face
   bucket publishing (SWE-bench#642). The bash-only board was folded into Verified
   (experiments `a3e196e`). The "SWE-bench family" now also lists ProgramBench and
   CodeClash.

**Reading:** treat Verified as a frozen, saturated reference. Numbers in it still order
older models, but they can't tell current frontier models apart and won't include new ones.

## 3. Selection criterion: issue → PR

Issue Agent runs mini-SWE-agent on **a real issue in an existing repository**, and success
means the project's own tests pass after the patch. A benchmark is only useful to us if it
measures that same task. Freshness alone doesn't qualify a benchmark: an up-to-date board
that measures something else would answer the wrong question.

## 4. Candidates

| Benchmark | Last update seen | Task shape | Fit |
|---|---|---|---|
| [SWE-rebench](https://swe-rebench.com/) | 2026-07-01 (window 2026-05-15 → 07-01, 111 tasks from 65 repos) | Issue → patch from real PRs merged inside each window | **Primary** |
| [SWE-bench Pro](https://labs.scale.com/leaderboard/swe_bench_pro_public) (Scale SEAL) | Undated; top entries are Muse Spark 1.1 and gpt-5.4 | Issue → patch on copyleft (public set) and proprietary (private set) repos | **Secondary** |
| SWE-bench Multilingual | Frozen (2026-02-20) | Issue → patch, includes JS/TS | Right shape, same freeze as Verified |
| [Terminal-Bench 4.0](https://www.vals.ai/benchmarks/terminal-bench-4) | 2026-10-16 (Vals AI) | Self-contained terminal tasks with per-task checks | **Not suitable** |
| [ProgramBench](https://www.vals.ai/benchmarks/programbench) | 2026-10-12 (Vals AI) | Build a program from scratch | **Not suitable** |

- **SWE-rebench.**
  - Same issue → patch format, with every window drawn from recently merged PRs, so models
    can't have trained on the solutions.
  - Runs a single fixed scaffold for every model, reports a ± interval and pass@5, and
    flags potential contamination.
  - Updates are irregular, roughly every few weeks to months.
- **SWE-bench Pro.**
  - Same task. The entries marked "Run with mini-swe-agent harness" use our own engine.
  - The industry has largely moved to it since OpenAI's switch.
  - The public board seems to lag the current model generation, so confirm before
    quoting it.
- **Terminal-Bench 4.0.**
  - The freshest board, and Vals AI runs it with mini-SWE-agent.
  - But its tasks are sysadmin, build and data chores, each graded by its own script.
    None is an issue in an existing codebase. It shows how well a model drives a bash
    loop, not whether it resolves issues.
- **ProgramBench.**
  - Generates a whole program, so it doesn't apply to us.

## 5. Recommendation

1. Use **SWE-rebench** as the primary public reference when choosing or changing `LLM_MODEL`.
2. Cross-check with **SWE-bench Pro**, preferring the mini-SWE-agent-harness entries.
3. Cite SWE-bench Verified only as historical context, and Terminal-Bench / ProgramBench
   not at all for this decision.
4. Public boards stay a coarse filter. The deciding signal is still our own audit log:
   resolve rate and cost per task on the repositories we actually serve.

## 6. Sources

Primary, as of 2026-10-18:

- swebench.com: <https://www.swebench.com/>
- SWE-bench repo: <https://github.com/SWE-bench/SWE-bench>
- Submissions repo and policy note: <https://github.com/SWE-bench/experiments>
- Leaderboard site repo: <https://github.com/SWE-bench/swe-bench.github.io>
- SWE-rebench: <https://swe-rebench.com/>
- SWE-bench Pro (Scale SEAL): <https://labs.scale.com/leaderboard/swe_bench_pro_public>
- Vals AI, SWE-bench Verified (archived): <https://www.vals.ai/benchmarks/swebench>
- Vals AI, Terminal-Bench 4.0: <https://www.vals.ai/benchmarks/terminal-bench-4>
- Vals AI, ProgramBench: <https://www.vals.ai/benchmarks/programbench>

Secondary:

- SWE-bench co-creator on Verified saturation (Hacker News): <https://news.ycombinator.com/item?id=47912620>
- OpenAI on moving to SWE-bench Pro: <https://x.com/OpenAIDevs/status/2026002219909427270>
- Latent Space, "The End of SWE-Bench Verified": <https://www.latent.space/p/swe-bench-dead>
