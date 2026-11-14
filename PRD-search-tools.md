# PRD — Code Search Tools in the Agent Sandbox

**Status:** Draft, not scheduled. Recorded to be revisited; nothing here is
implemented yet.
**Date:** 2026-11-14
**Type:** `feat` (agent tooling: container image, system prompt, tests; no
change to the harness flow)

## Revision history

| Date | Change |
|---|---|
| 2026-11-14 | Original version. Tool facts (versions, sizes, maintenance) come from public release pages and package indexes on this date; sizes are approximate and are measured in phase ST1 before anything depends on them. |

---

## 1. Vision and goals

The coding agent is mini-SWE-agent's `DefaultAgent`, and its only tool is
`bash` (see the README's "Bash only" bullet). Everything it learns about a
target repository comes from shell commands: `grep -r`, `find`, `cat`,
`sed -n`, `git log -S`. That works, but it is noisy in two ways that cost
steps and tokens:

- **`grep -r` does not honor `.gitignore`.** Before the agent starts, the
  harness provisions the toolchain and installs dependencies (`npm ci` etc.,
  `runner/deps.py`), so a recursive grep in a Node repository descends into
  `node_modules`. The observation cap (`runner/templates.py`) then elides the
  output, and the agent spends another step narrowing the search.
- **There is no structural search.** "Every call of `foo`", "the definition
  of class `Bar`", or "this pattern, but not inside comments or strings" are
  approximated with regexes, which match too much or too little. Broad edits
  done with `sed` are fragile.

The proposal is to add two command-line tools to the image and teach the agent
to use them in a short prompt section. This stays inside the bash-only design:
they are just more shell commands.

### Goals

- **G1.** `rg` (ripgrep) and `ast-grep` are on the agent's `PATH` inside the
  sandbox, at pinned versions with verified checksums.
- **G2.** A short "SEARCHING THE CODE" section in `prompts/system.md` (and a
  read-only variant in `prompts/system_diagnose.md`) says when to use which
  tool, with a few canonical examples and a fallback rule.
- **G3.** The prompt's examples are executed by a test against a fixture repo,
  and CI checks that both binaries run inside the built image.

### Non-goals

- Indexed or semantic search (embeddings, BM25 indexes), LSP servers, MCP.
- Any change to mini-swe-agent or to the agent's tool schema.
- Changing the harness flow (clone, toolchain, test re-run, commit, push).

## 2. Current state

| Area | Today | File |
|---|---|---|
| Search guidance | Rule 9 of OPERATION POLICY: "Use bash commands for everything: read files with `cat`, edit with `sed`/`awk`/`python -c`, navigate with `find`, list with `ls`." | `prompts/system.md` |
| Image packages | `git curl ca-certificates build-essential` on `python:3.12-slim`; no `rg`, no `ast-grep` | `Dockerfile` |
| Agent shell | Allowlisted env, `PATH` falls back to `_DEFAULT_PATH` (includes `/usr/local/bin`); every command runs under `nice -n 19`, `command_timeout_seconds` (120 s default) | `src/issue_agent/runner/environment.py` |
| Output size | Observations over `llm.observation_max_chars` (10000 default) are elided to head and tail, with a hint to use `head`/`grep` more selectively | `src/issue_agent/runner/templates.py` |
| Workspace | Removed and re-cloned for every task, so any per-repo index would be rebuilt every time | `src/issue_agent/runner/workspace.py` |
| VM | 1 shared vCPU, about 1 GB RAM, shared with the web server and the worker | `fly.toml`, `prompts/system.md` RESOURCE LIMITS |
| CI image check | The `docker` job only runs `docker build`; nothing runs inside the image | `.github/workflows/ci.yml` |

## 3. Tool assessment

Baseline image size: about 700 MB (`python:3.12-slim`, about 120–150 MB, plus
the venv, which the `Dockerfile` puts at about 550 MB). None of the
command-line tools below runs a daemon, so their idle CPU and memory cost is
zero. They cost something only while a command runs.

| Tool | Verdict | Image size impact | CPU impact | Memory impact | Benefits / risks |
|---|---|---|---|---|---|
| **ripgrep (`rg`)** | **Adopt.** Very mature, actively maintained (15.0 Oct 2025, 15.1, 15.2 Jul 2026). | About +5 MB (<1%) | Lower than `grep -r` in practice: skips gitignored trees such as `node_modules` and build output. Multithreaded, which is neutral on 1 vCPU under nice 19. | A few MB to tens of MB, for the duration of the command | **Benefits:** faster, less noisy search, fewer tokens; `rg --files -g` replaces most `find` use; models already know it well. **Risks:** it can hide an ignored file that matters (mitigation: `-uu`). |
| **ast-grep** | **Adopt.** Very active (0.40–0.43 during 2026), large community, on the Thoughtworks Tech Radar. | About +40–45 MB (~6%); bundles every tree-sitter grammar | Higher than rg per file because it parses; seconds when scoped to a subtree. Multithreaded. | Tens of MB up to about 200 MB on large trees, during the command | **Benefits:** real structural search (calls, definitions, metavariable patterns, ignores comments and strings), safe rewrites (`--rewrite`), an outline of symbols. **Risks:** the pattern syntax is less familiar to models, which can waste steps. **`/usr/bin/sg` already exists on Debian (the `login` package)**, so it must be exposed only as `ast-grep`. |
| **zvec-grep (`zg`)** | **Not now.** Active but very young: open source since Aug 2026, v0.3.0 on 2026-11-03 rewrote the core in Rust. The README warns that CLI, config and index format may still change. | About +150–250 MB: needs Node ≥ 22 in the image (the toolchain Node lives on the volume and follows each repo's version), plus the package, the native zvec library and the embedding model (`potion-code-16M-v2`, about 33 MB) | High per task: the workspace is re-cloned, so indexing and embedding run from scratch every time, on the single vCPU, next to `npm ci`/`tsc`/`jest` | The project's own heuristic is about 150 MiB per model, not guaranteed; real OOM risk on a 1 GB VM (v0.3.0 claims about 53% lower peak) | **Benefits:** hybrid BM25 + vector + rg search for conceptual questions; the vendor reports fewer than half the tool calls on SWE-QA-Bench (not independently verified). **Risks:** writes `.zvec-grep/` inside the workspace, which the harness would commit; downloads the model at runtime unless it is baked in (against the prompt's network rule); its MCP integration is unusable with a bash-only agent. |
| **universal-ctags** | **Maybe later, low priority.** Moderate maintenance: 6.2 is from May 2025; CVE-2026-2641 (V parser recursion) has no confirmed fix. | About +3–5 MB (Debian package) | Low; `ctags -R` takes seconds, but is rebuilt per task | Low | **Benefits:** a definitions index for 100+ languages. **Risks:** overlaps with ast-grep's outline and rg; models rarely use it unprompted; a `tags` file in the workspace could get committed. |
| **fd** | **No.** Active but redundant. | About +3 MB | Low | Low | `rg --files -g '<glob>'` covers it; one more thing for the prompt to explain. |
| **LSP servers (pyright, tsserver, gopls…)** | **No.** Active projects, wrong fit. | +100–300 MB per language (pyright also needs Node) | High at startup. Each mini command is a new process, so a long-lived server cannot survive between steps | 200 MB to over 1 GB on mid-size projects; high OOM risk | **Benefits:** precise references and types. **Risks:** nowhere to keep a server alive; the batch equivalent already exists through the project's own checkers (`tsc --noEmit`, `mypy`), which the prompts already encourage. |
| **Semgrep CE** | **No.** Active (Semgrep Inc.), but heavy and security-rule oriented. | About +100–200 MB (Python wheel + OCaml binary) | Slow, costly startup | Hundreds of MB | Overlaps with ast-grep, which is lighter and faster for structural search and rewrite; the official rules have a restrictive license. |

Sources (checked 2026-11-14):
[ripgrep releases](https://github.com/BurntSushi/ripgrep/releases) ·
[ripgrep 15.2.0](https://newreleases.io/project/github/BurntSushi/ripgrep/release/15.2.0) ·
[ast-grep](https://github.com/ast-grep/ast-grep) ·
[ast-grep on the Tech Radar](https://www.thoughtworks.com/radar/tools/ast-grep) ·
[ast-grep Arch package (size)](https://archlinux.org/packages/extra-testing/x86_64/ast-grep) ·
[zvec-grep](https://github.com/zvec-ai/zvec-grep) ·
[zvec-grep v0.3.0](https://github.com/zvec-ai/zvec-grep/releases/tag/v0.3.0) ·
[zvec-grep embedding docs](https://github.com/zvec-ai/zvec-grep/blob/main/docs/07-embedding.md) ·
[potion-code-16M-v2](https://huggingface.co/minishlab/potion-code-16M-v2/tree/main) ·
[universal-ctags v6.2.0](https://newreleases.io/project/github/universal-ctags/ctags/release/v6.2.0) ·
[CVE-2026-2641](https://ubuntu.com/security/CVE-2026-2641)

## 4. Design

### 4.1 Dockerfile

Install the upstream release binaries in the `base` stage, before
`USER agent`:

- `ARG RG_VERSION` / `ARG AST_GREP_VERSION`, plus the matching SHA-256 values
  for amd64 and arm64 (`TARGETARCH`; Fly builds amd64, while arm64 keeps local
  builds on Apple silicon working).
- ripgrep: the static musl tarball (`ripgrep-<v>-x86_64-unknown-linux-musl.tar.gz`
  / `aarch64-unknown-linux-musl`), keep only the `rg` binary.
- ast-grep: the release zip (`app-x86_64-unknown-linux-gnu.zip` /
  `app-aarch64-unknown-linux-gnu.zip`), keep only the `ast-grep` binary.
  **Never create an `sg` symlink:** `/usr/bin/sg` is Debian's
  switch-group command.
- Verify each download with `sha256sum -c`; fail the build on mismatch.
- Install into `/usr/local/bin`. That directory is on the image's `PATH` and in
  `_DEFAULT_PATH`, so the sandbox finds both binaries with no change to
  `runner/environment.py`.
- Download with the `curl` that is already installed, in a single `RUN` that
  removes its temporary files, so no extra layer carries the archives.
  Extracting the zip needs `unzip`, or Python's `zipfile` to avoid adding a
  package.

Alternatives considered:

| Option | Pros | Cons |
|---|---|---|
| **Release binaries + checksums (chosen)** | Latest versions, exact pin, no new apt/PyPI dependency, same versions in CI and the image | Two checksums to bump per upgrade |
| Debian `ripgrep` apt package | One word in the existing `apt-get` line | Older 14.x line; version follows the base image, not us |
| PyPI `ast-grep-cli` via uv | Pinned in `uv.lock`, upgraded with the other deps | Puts an agent tool in the service's own dependency set and venv; still no rg |

Expected growth: about +50 MB (about 7%). ST1 measures it with
`docker image inspect` before and after and records the number here.

### 4.2 Prompt section

Replace the search part of rule 9 ("navigate with `find`, list with `ls`")
with a pointer to the new section, keeping the rest of the rule (read with
`cat`, edit with `sed`/`awk`/`python -c`). Add the section after OPERATION
POLICY. Draft:

```text
SEARCHING THE CODE:
- Default to `rg` (ripgrep) for text, names and literals: `rg -n 'parse_config' src/`,
  `rg --files -g '*.ts'`. It skips gitignored paths (node_modules, build output);
  add `-uu` only when you need them. Use `-F` for literal strings.
- Use `ast-grep` only when you need the code's structure: every call of a function,
  a definition, a pattern that grep would match inside comments or strings.
  Never call it `sg` — that is a different program here.
    ast-grep run --lang python -p 'requests.get($URL, $$$ARGS)' src/
    ast-grep run --lang ts -p 'function $NAME($$$) { $$$ }' src/
    ast-grep run --lang ts -p 'console.log($$$)' -r '' src/   # preview a rewrite; add -U to apply
  The pattern must be valid code in that language; `$X` matches one node, `$$$X` many.
  Quote patterns in single quotes. If a pattern finds nothing, simplify it or fall
  back to rg — do not spend more than two attempts on one pattern.
- `ast-grep run --help` documents the rest.
```

Constraints:

- Keep it at roughly 15–20 lines. The system prompt is already long, and a big
  section would dilute the rules that matter most (git, security, completion
  contract).
- **rg is the default.** The wording must not invite ast-grep where rg is
  enough. The two-attempt cap is what stops the agent from debugging patterns
  for many steps.
- Forge-neutral, like every file in `prompts/` (`tests/unit/test_prompts.py`).
- `prompts/system_diagnose.md` gets the same section minus the rewrite example
  and `-U`, because diagnosis is read-only.
- Re-check the exact flags against the pinned version in ST2 (e.g. whether
  `--rewrite` previews or needs `-U`/`--update-all` to write).

### 4.3 Why inline, not a skill file

mini-SWE-agent has no skill mechanism and no on-demand loading. The agent sees
the system prompt and whatever it chooses to `cat`. A cheat-sheet file shipped
in the image (e.g. `/app/prompts/tools/ast-grep.md`) with a "read this before
using ast-grep" line costs a step, and models often skip it. An inline section
of about 300–400 tokens is more reliable. With `llm.prompt_caching:
default_end` it sits in the cached prefix, so its per-step cost is small.

Revisit if the guidance grows past a short section, e.g. `ast-grep scan` YAML
rules or per-language recipes. At that point a separate file that the prompt
points to becomes worth the extra step.

## 5. Testing

- **Unit** (`tests/unit/test_prompts.py`):
  - the "SEARCHING THE CODE" section exists in `system.md` and
    `system_diagnose.md`;
  - it names `ast-grep` and never tells the agent to run `sg` (no `` `sg `` or
    `sg run` in any prompt file);
  - it states the fallback rule (rg as default, attempt cap);
  - rule 9 no longer prescribes `find` for navigation;
  - the diagnose variant has no `-U` or rewrite example.
- **Integration** (new `tests/integration/test_search_tools.py`):
  - parse the example commands out of the prompt section, so the test follows
    the prompt instead of duplicating it;
  - run each one through `SandboxedLocalEnvironment` against a small fixture
    repo (a Python file with a `requests.get(...)` call, a TS file with a
    function and a `console.log`, a `.gitignore` excluding `node_modules/`, and
    a matching file inside `node_modules/`);
  - assert the expected matches, that `rg` skips the ignored file and `rg -uu`
    finds it, and that the rewrite example does not modify files without `-U`;
  - skip with an explicit reason when the binaries are missing on a developer
    machine, but **fail in CI**: the CI `test` job installs the same pinned
    versions (shared version/checksum values, e.g. read from the Dockerfile
    `ARG`s, so they cannot drift).
- **Image** (`.github/workflows/ci.yml`, `docker` job): after `docker build`,
  run `docker run --rm fly-swe:ci sh -c 'rg --version && ast-grep --version'`,
  plus a check that `sg` is not ast-grep (`! sg --version 2>/dev/null | grep -q ast-grep`).
- **Coverage:** no `src/` change is expected, so the 90% floor is unaffected.
  If a helper lands in `src/`, it gets tests like any other code.

## 6. Rollout and measurement

No feature flag: the binaries are inert until the agent calls them, and the
prompt change is the only behavior change. After about 20 real tasks, mine
`/data/events/<task>/trajectory.json` for:

- how often `rg` and `ast-grep` are called, and how often `grep -r`/`find`
  still appear;
- ast-grep calls that fail or return nothing;
- steps and cost per task compared with a sample from before the change;
- observations elided by the output cap during search commands.

Then decide whether to keep, trim, grow (more examples) or drop the ast-grep
part of the section.

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| The agent over-uses ast-grep and burns steps on patterns | rg is stated as the default; two-attempt cap; measured in §6 |
| Prompt examples drift from the installed version | Versions pinned with checksums; the integration test runs the examples verbatim |
| `sg` name clash on Debian | Install only as `ast-grep`; prompt says never `sg`; CI checks it |
| Image growth slows cold starts | About +50 MB (about 7%), measured in ST1; single layer, no archives left behind |
| rg hides an ignored file that matters | `-uu` documented in the section |
| Upstream supply chain | Official release assets only, SHA-256 verified, versions bumped deliberately |

## 8. Implementation phases

| Phase | Scope | Done when |
|---|---|---|
| **ST1** | Dockerfile install (§4.1) + CI image check | Image builds for amd64/arm64; `rg`/`ast-grep` run as UID 1000 inside it; size delta recorded in this PRD |
| **ST2** | Prompt section in `system.md` and `system_diagnose.md` (§4.2) + unit tests | `test_prompts.py` passes; flags re-checked against the pinned version |
| **ST3** | Example integration test + CI install step (§5) | The test runs (not skipped) in CI and fails if an example breaks |
| **ST4** | Docs: README "Bash only" bullet mentions the bundled search tools; `docs/operations.md` notes the versions and how to bump them. Version bump (`minor`, `feat`) | Docs merged with the code |

## 9. Future / out of scope

- **zvec-grep**, to revisit when: a stable (1.x) release exists; the index can
  live outside the workspace or be excluded from git (`.git/info/exclude`)
  so the harness never commits it; the model ships in the image rather than
  being downloaded at runtime; Node 22 is available without bloating the main
  image; and indexing fits the 1 vCPU / 1 GB budget alongside a dependency
  install. It would ship behind a per-repo flag first.
- **universal-ctags**, if trajectories show the agent struggling to find
  definitions that ast-grep's outline does not cover.
- **`ast-grep scan` with YAML rules**, and per-language recipes in a separate
  cheat-sheet file (§4.3).
- **Persistent per-repo caches** on the Fly volume, which any index-based tool
  would need, since the workspace is re-cloned for every task.
