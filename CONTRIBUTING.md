# Contributing to fly-swe

Thanks for considering a contribution. This file covers the human side of
development; [AGENTS.md](./AGENTS.md) carries the equivalent instructions
for coding agents working in this repo.

Everyone taking part in this project is expected to follow the
[Code of Conduct](./CODE_OF_CONDUCT.md).

## Getting set up

- **Python** 3.12+
- **uv** 0.11+ ([install](https://docs.astral.sh/uv/getting-started/installation/))
- **git** + **GitHub CLI** (`gh`)
- **flyctl** ([install](https://fly.io/docs/hands-on/install-flyctl/)) — only needed if you're testing a deploy
- **smee-client** for local webhook testing (`npm install -g smee-client`)

```bash
git clone https://github.com/stateway-io/fly-swe.git
cd fly-swe
uv sync                      # installs runtime + dev dependencies
cp .env.example .env         # fill in local secrets
cp config.yaml.example config.yaml   # set repos:/llm.model for your test repo
uv run honcho start          # runs web + worker locally
```

See the [README](./README.md#quickstart--deploy-in-10-minutes) for the
GitHub App setup that local webhook testing needs, and
[docs/operations.md](./docs/operations.md) for everything operational.

## Making a change

```bash
uv run ruff check          # lint
uv run pytest              # unit + integration tests
uv run pytest tests/unit/test_config.py -v   # a single test file
```

- Every behavioral change needs a test in `tests/unit/` or
  `tests/integration/`. Tests that hit a real LLM or GitHub go in
  `tests/e2e/` and are opt-in (`pytest -m e2e`).
- `docs/operations.md` § *The Agent Execution Contract* is a hard invariant
  if you're touching `prompts/` or `runner/executor.py`: the coding agent
  edits files and leaves them in the working tree; the harness — never the
  agent — owns branch, commit, push, and PR. `tests/unit/test_prompts.py`
  locks this; run it after any prompt edit.
- Match the structure and idiom of the file you're editing.
- Adding support for another forge (GitLab, Gitea, Gogs, …)? Start with
  [docs/providers.md](./docs/providers.md).

## Git conventions

- **Commits & PR titles:** [Conventional Commits](https://www.conventionalcommits.org/)
  — `type: concise description`, e.g. `fix: validate HMAC on empty payload`.
  The PR title should match its lead commit.
- **Branches:** short-lived, created from `origin/main`, prefixed to match
  the commit type (`feat/<slug>`, `fix/<slug>`, `docs/<slug>`, …).
- **Merging:** `main` is the only long-lived branch, protected, squash-merge only.
- Full detail in [AGENTS.md § Git Convention](./AGENTS.md#git-convention).

## Opening a PR

1. Fork or branch, make your change with tests, and confirm
   `ruff check` and `pytest` are green.
2. Open a PR against `main`. CI (`.github/workflows/ci.yml`) runs lint,
   unit + integration tests, and a Docker build.
3. One review is required before merge.

## Reporting bugs / requesting features

Open a GitHub issue. If you're comfortable, label it yourself following
[docs/label-schema.md](./docs/label-schema.md) — that's the same schema
fly-swe itself uses to route issues.

## Security

Please don't open a public issue for a security vulnerability — see
[SECURITY.md](./SECURITY.md) for how to report one privately.

## License

fly-swe is licensed under the [AGPL-3.0](./LICENSE). By submitting a
contribution you agree that it is licensed under the same terms.
