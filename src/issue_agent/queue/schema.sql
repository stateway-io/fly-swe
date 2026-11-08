CREATE TABLE processed_deliveries (
  provider_instance TEXT NOT NULL DEFAULT 'github',
  delivery_id   TEXT NOT NULL,
  received_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  event_type    TEXT NOT NULL,
  action        TEXT,
  PRIMARY KEY (provider_instance, delivery_id)
);

CREATE TABLE tasks (
  id              TEXT PRIMARY KEY,
  delivery_id     TEXT NOT NULL,
  repo            TEXT NOT NULL,
  issue_number    INTEGER NOT NULL,
  trigger_kind    TEXT NOT NULL,
  label_category  TEXT,
  status          TEXT NOT NULL,
  pr_number       INTEGER,
  cost_usd        REAL NOT NULL DEFAULT 0,
  iterations      INTEGER NOT NULL DEFAULT 0,
  retry_count     INTEGER NOT NULL DEFAULT 0,
  error           TEXT,
  created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at      TEXT,
  completed_at    TEXT,
  heartbeat_at    TEXT,
  target_repo     TEXT NOT NULL DEFAULT '',
  base_branch     TEXT NOT NULL DEFAULT 'main',
  workflow        TEXT NOT NULL DEFAULT 'trunk_based',
  trigger_json      TEXT,
  provider_instance TEXT NOT NULL DEFAULT 'github',
  credentials_ref   TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_unique_pending ON tasks(provider_instance, repo, issue_number, trigger_kind) WHERE status = 'pending';
CREATE INDEX idx_tasks_status ON tasks(status, created_at);
CREATE INDEX idx_tasks_heartbeat ON tasks(status, heartbeat_at) WHERE status = 'running';

CREATE TABLE task_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id       TEXT NOT NULL REFERENCES tasks(id),
  event_type    TEXT NOT NULL,
  payload_json  TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_task_events_task ON task_events(task_id, created_at);

CREATE TABLE daily_budget (
  date           TEXT NOT NULL,
  scope          TEXT NOT NULL DEFAULT 'global',
  total_cost_usd REAL NOT NULL DEFAULT 0,
  task_count     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (date, scope)
);

-- Per-issue work state: one row per (provider_instance, issue_repo,
-- issue_number), spanning as many tasks as the work takes. Introduced by
-- _migrate_v4 for pre-existing DBs — a pure additive
-- CREATE TABLE — and rebuilt by _migrate_v7 to key it by instance.
CREATE TABLE issue_work (
  provider_instance TEXT    NOT NULL DEFAULT 'github',
  issue_repo        TEXT    NOT NULL,
  issue_number      INTEGER NOT NULL,
  target_repo       TEXT    NOT NULL,
  base_branch       TEXT    NOT NULL,
  work_branch       TEXT    NOT NULL,
  state             TEXT    NOT NULL,          -- open | closed
  closed_reason     TEXT,                      -- see PRD §3.3
  attempts          INTEGER NOT NULL DEFAULT 0,
  last_task_id      TEXT,
  last_error        TEXT,
  last_summary      TEXT,
  last_summary_kind TEXT    NOT NULL DEFAULT '',  -- '' | mechanical | llm | submission
  pr_number         INTEGER,
  total_cost_usd    REAL    NOT NULL DEFAULT 0,
  total_iterations  INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (provider_instance, issue_repo, issue_number)
);

CREATE INDEX idx_issue_work_open ON issue_work(state, updated_at) WHERE state = 'open';

-- Forge state of every PR a task or issue_work row points to — the
-- data-retention sweep's cache (merged/closed rows are never re-checked).
-- `closed_at` is when it was merged (state=merged) or closed unmerged.
-- Introduced by _migrate_v9, a pure additive CREATE TABLE.
CREATE TABLE pr_state (
  provider_instance TEXT    NOT NULL,
  repo              TEXT    NOT NULL,          -- the target repo the PR lives on
  pr_number         INTEGER NOT NULL,
  state             TEXT    NOT NULL,          -- open | merged | closed
  closed_at         TEXT,
  checked_at        TEXT    NOT NULL,
  PRIMARY KEY (provider_instance, repo, pr_number)
);
