<!--
Title: Conventional Commits, matching the lead commit —
`type: concise description [#issue]` (see CONTRIBUTING.md § Git conventions).
-->

## Summary

<!-- What changes and why. Link the issue it closes or advances, if any. -->

## Test plan

<!-- How you verified the change: tests added or updated, commands run. -->

## Checklist

- [ ] `uv run ruff check`, `uv run ruff format --check` and `uv run pytest --cov` pass (coverage ≥ the `pyproject.toml` floor)
- [ ] Behavioral changes have tests in `tests/unit/` or `tests/integration/`
- [ ] `version` bumped in `pyproject.toml` and `uv.lock` synced (`uv lock`)
- [ ] Docs updated where behavior or configuration changed (`docs/providers.md` if a provider port changed)
- [ ] No forge names ("GitHub", "Pull Request", "PR") added to `prompts/` — use `{{ terms.* }}`
