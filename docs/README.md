# Documentation

| Document | What it covers |
|---|---|
| [operations.md](./operations.md) | Operations runbook — configuration, GitHub App setup, onboarding a repo pair, deploy/release/rollback, monitoring, logs, queue management, budgets, volumes, backups, credential rotation. |
| [troubleshooting.md](./troubleshooting.md) | Symptom-based playbooks: stuck tasks, `/readyz` failures, rate limits, budget aborts, git failures, duplicate PRs, and more. |
| [label-schema.md](./label-schema.md) | The label contract a target repo opts into — which labels route to a fix, a read-only diagnosis, a triage comment, or a skip. |
| [trigger-conditions.md](./trigger-conditions.md) | Precisely when the agent fires vs. stays silent — the full webhook→queue→worker pipeline, current gaps, and recommended fixes. |
| [providers.md](./providers.md) | Writing a provider plugin for a new forge — the ports, the canonical model, settings, registration, and the conformance suite. |
| [deploy/telegram-webhook.md](./deploy/telegram-webhook.md) | Wiring a Discord or Telegram webhook for budget-exceeded alerts. |
| [research/agent-landscape.md](./research/agent-landscape.md) | *(Historical snapshot)* Survey of reusable coding-agent engines and deploy targets considered for this project. |
| [research/mini-swe-agent-api.md](./research/mini-swe-agent-api.md) | *(Historical snapshot)* A reference dig through the mini-swe-agent API as integrated here. |
| [research/entire.md](./research/entire.md) | *(Snapshot)* What Entire (agent session capture + forge) is, whether it fits fly-swe, where it would plug in, and mini-swe-agent compatibility. |

See the [README](../README.md) for the project overview, quickstart, and
configuration summary; [AGENTS.md](../AGENTS.md) for instructions to coding
agents working in this repo; and [CONTRIBUTING.md](../CONTRIBUTING.md) for
human contributor guidelines.
