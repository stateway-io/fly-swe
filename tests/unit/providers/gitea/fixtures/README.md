# Gitea webhook fixtures

Raw deliveries captured from **Gitea 28.4.0** (`gitea/gitea:28.4.0`) by
[`capture.py`](./capture.py). Each `<name>.json` file holds the request body
byte for byte. `headers/<name>.json` holds that delivery's `X-*` headers. With
the capture secret (`capture-webhook-secret`), the recorded
`X-Gitea-Signature` verifies against the body. The kit and the tests re-sign
the bodies with their own secret.

`measurements/<version>.json` holds what `capture.py` measured for PRD-Gitea
§10 Q1–Q5. That covers the replay delivery id, the label-event headers, token
scopes, the permission endpoint and `mergeable` over time. Measurements exist
for 28.4.0 and for 1.27.3 (`--measure-only`). The PRD's revision history
summarizes them.

`api/pull_{open,merged,closed}.json` are `GET /repos/team/app/pulls/{n}`
responses from the same seeded instance (`capture.py --measure-only --keep`).
PR #2 is open, #5 was merged and #4 was closed through the API as `root`.
`pr.py`'s tests use them for the PR shape: `head.ref`, `head.sha`,
`head.repo.full_name`, `mergeable`, `merged`, `merged_at` and `closed_at`.

## Regenerating

```bash
uv run python -m tests.unit.providers.gitea.fixtures.capture --image gitea/gitea:28.4.0
uv run python -m tests.unit.providers.gitea.fixtures.capture --image gitea/gitea:1.27.3 --measure-only
```

You need Docker and git. The script starts a throwaway container (`fly-swe-gitea-capture`
on `127.0.0.1:3300`), receives the webhooks on port 3399 of the Docker bridge
gateway (`host.docker.internal` inside the container), and removes the
container when it's done. Neither is reachable from the network: the test
password in `tests/helpers/gitea_server.py` is public. Ids,
UUIDs, timestamps and signatures differ between runs, and the
`mergeable` samples are timing-dependent.

## Contents

All data is synthetic, so nothing needed scrubbing:

- users `root`, `issue-agent` (the bot), `maintainer` and `outsider`, with
  `@example.com` addresses;
- org `team` and repo `team/app`;
- `ROOT_URL=https://git.example.com/`.

The API URLs use `ROOT_URL`. The `html_url` fields use the host the request
came in on (`http://localhost:3300`).

| Fixture | `X-Gitea-Event` / `X-Gitea-Event-Type` / `action` |
|---|---|
| `issues_opened` | `issues` / `issues` / `opened` |
| `issues_label_updated_without_trigger` | `issues` / `issue_label` / `label_updated` (labels: `wontfix`) |
| `issues_label_updated_with_trigger` | `issues` / `issue_label` / `label_updated` (labels: `agent-fix-please`, `wontfix`) |
| `issues_label_cleared` | `issues` / `issue_label` / `label_cleared`. **Note:** `issue.labels` still lists the labels from *before* the clear. |
| `issues_closed`, `issues_reopened` | `issues` / `issues` / `closed`, `reopened` |
| `issue_comment_created` | `issue_comment` / `issue_comment` / `created` (by `maintainer`) |
| `issue_comment_own_bot` | `issue_comment` / `issue_comment` / `created` (by `issue-agent`) |
| `issue_comment_edited` | `issue_comment` / `issue_comment` / `edited` |
| `issue_comment_on_pull_request` | `issue_comment` / `pull_request_comment` / `created`, with `is_pull: true`. Delivered only when the hook's **Pull Request Comment** toggle is on. |
| `push` | `push` / `push` |
