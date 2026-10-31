# Plan: high availability across regions with LiteFS

Status: **proposed, not scheduled.** Nothing in this document has been built
or run. It exists so the decision can be taken later with the facts in hand.

Written 2026-10-17, the day after the outage described below. The immediate
recovery procedure is in
[operations.md § Recovering from a host failure](../operations.md#recovering-from-a-host-failure-restore-a-snapshot-recreate-the-machine);
this plan is about not needing it.

## 1. Context

fly-swe runs as **one Fly Machine with one volume** (`issue_agent_data`, 3 GB,
region `gru`). The volume holds the SQLite database (task queue, per-issue work
state, daily budget, processed webhook deliveries), the agent's workspaces and
the audit logs.

On 2026-10-16 the physical host of that Machine and volume became unreachable
during a Fly platform incident (rolling host reboots). The consequences were
exactly what the architecture implies:

- the service was down, and the webhooks GitHub sent meanwhile failed (GitHub
  does not redeliver failed deliveries by itself);
- `flyctl deploy` could not replace the Machine: *"requires manual intervention,
  it can't be automatically replaced because its volume … is on an unreachable
  host"*;
- the only recovery path is restoring a daily snapshot onto a new Machine,
  which loses up to ~24 h of state and is a manual procedure that had never
  been rehearsed.

| | Today |
|---|---|
| RPO (data lost on a host failure) | up to ~24 h (daily snapshot; 5-day retention) |
| RTO (time to recover) | the host's outage, or minutes of manual work if a snapshot is restored |
| Failover | none; manual |

## 2. Goals and non-goals

**Goals**
- A host or regional failure no longer takes the service down for more than a
  few minutes, without an operator.
- Losing at most the last few seconds of state on failover instead of up to a
  day.
- Keep SQLite, a single queue and the existing
  "claim a task atomically" logic (`BEGIN IMMEDIATE`) as they are.

**Non-goals**
- Horizontal scaling or running tasks on several Machines at once. One agent
  works at a time, as today.
- Active-active writers.
- Changing the agent's behavior, prompts or GitHub flow.

## 3. Why two plain Machines do not work

Adding a second Machine is the obvious idea and it makes things worse. Fly's
proxy sends each request to **one** Machine (the least loaded and closest;
random on a tie) and GitHub delivers each webhook once, so one Machine receives
it. But a Fly volume attaches to a single Machine and is **not replicated**, so
two Machines are two volumes and **two independent databases**:

| What the code does | Why it breaks with two databases |
|---|---|
| `processed_deliveries` dedupes webhooks by `delivery_id` (`queue/store.py`, `webhook/routes.py`) | A redelivery that lands on the other Machine is processed again. |
| `insert_task_unless_active_creation_task`, the partial unique index `idx_tasks_unique_pending` and `fetch_next`'s `BEGIN IMMEDIATE` | They serialize **within one database**. GitHub fires `issues.opened` and `issues.labeled` almost together; on two Machines both can enqueue a task for the same issue, and two agents work it, force-pushing the same branch and opening duplicate PRs/comments. |
| `issue_work` (branch, attempts, last summary) | A comment that lands on the other Machine finds no work state. |
| `daily_budget` | Counted per Machine: the daily cap effectively doubles. |
| Queue and orphan recovery (`worker.run_orphan_recovery`) | Each worker only sees its own database. Tasks queued on a dead Machine stay stuck, and the survivor never recovers them. |

So two plain Machines give you **no failover** (the state is on the dead one)
**and** the risk of duplicated work. Making a second Machine useful needs a
single source of truth with a single writer.

## 4. What LiteFS is

[LiteFS](https://docs.fly.io/litefs) is **open-source software**, not a Fly
managed service: a distributed file system that transparently replicates SQLite
databases. Fly hosts the documentation, but the docs say the team **cannot
provide support** for it, that the project is **pre-1.0** ("APIs may change and
features could be removed"), and that it is "stable and running in production
environments". It recommends regular off-site backups regardless.

How it works ([How LiteFS works](https://docs.fly.io/litefs/how-it-works)):

- **Interception.** LiteFS is a passthrough file system (FUSE) mounted at a
  directory such as `/litefs`. The application opens `/litefs/issue_agent.db`
  as a normal SQLite file; LiteFS intercepts the file calls and records each
  transaction (in WAL mode, by reading the new frames) as an **LTX** file.
- **Single writer.** SQLite allows one writer, so exactly **one node is the
  primary** and all writes must go to it. It streams the LTX files to the
  other nodes, which keep a full local copy. Replicas are read-only.
- **Leases.** The primary is whoever holds a lease. `lease.type: consul` uses a
  Consul key (TTL minimum 10 s, lock-delay ~1 s in the examples) and lets the
  cluster elect a new primary when it dies. `lease.type: static` has no
  election: the primary is fixed by configuration (`candidate: true` on one
  node, `false` on the others).
- **Asynchronous replication.** The primary does not wait for replicas, which
  the docs describe as trading "some durability" for write throughput. A
  primary that dies can hold transactions that never left it, **and those are
  lost**. Synchronous replication is listed as planned, not available.
- **Split brain is detected.** Each database has a position (a transaction id
  plus a rolling checksum). A node that diverges is detected and automatically
  re-snapshotted from the new primary, discarding its unreplicated writes.
- **Detecting the role** ([primary docs](https://docs.fly.io/litefs/primary)).
  The file `/litefs/.primary` exists **only on a replica that is connected to
  the primary** and contains the primary's hostname. If it is absent the node
  is the primary **or** a replica that cannot reach the primary — the two are
  not distinguishable from the file alone.
- **Built-in proxy** ([proxy docs](https://docs.fly.io/litefs/proxy)). Optional,
  disabled by default. In front of the app it forwards write requests
  (`POST`, `PUT`, …) arriving at a replica to the primary using Fly's
  `fly-replay` header, and makes `GET`s on a replica wait until the replica has
  caught up to a position kept in a cookie (read-your-writes). The app must not
  write on `GET`, and clients must keep cookies. `primary-redirect-timeout`
  holds writes while no primary is available.
- **On Fly** ([getting started](https://docs.fly.io/litefs/getting-started-fly)).
  Install `fuse3`, copy the `litefs` binary, run LiteFS as the container's
  entrypoint (`litefs mount`), keep `data.dir` on a persistent volume, use
  `fly consul attach` for the Consul lease. The database must live in the
  **root** of the mount (no subdirectories). Docs advise **against combining
  LiteFS with Fly's autostop/autostart**.

It solves the volume limit by **copying**, not by sharing: each Machine keeps
its own volume, and LiteFS keeps the copies in step.

## 5. Target architecture

Two Machines, one per region, one volume each, both running the whole app
(web + worker); only the primary's worker runs tasks.

```
GitHub ──POST──▶ Fly Proxy ──▶ nearest healthy Machine
                                   │
                     ┌─────────────┴──────────────┐
              Machine A (gru)               Machine B (region 2)
              LiteFS proxy :8080            LiteFS proxy :8080
              PRIMARY (holds lease)         REPLICA (read-only)
              web + worker running          web up, worker idle
              /litefs/issue_agent.db  ⇐ LTX stream ⇒  /litefs/issue_agent.db
                     ▲                              │
                     └───── POST on B, fly-replay ──┘
```

- **Webhooks.** GitHub's POST goes to whichever Machine Fly picks. On a
  replica the LiteFS proxy replays it to the primary, where the existing
  handlers run unchanged. GitHub's client has no cookies, which only matters
  for read-your-writes and does not affect a webhook POST.
- **Queue.** Only the primary's worker calls `fetch_next`. The atomic claim,
  the unique index and the dedupe table keep working because there is one
  writer.
- **Failover (automatic lease).** Host A dies → its lease expires (≥ 10 s) → B
  takes it and becomes primary with the database as of the last replicated
  transaction → B's worker starts → after `worker.orphan_timeout_seconds`
  (default 90 s) `run_orphan_recovery` re-queues the tasks that were `running`
  on A, and they resume from the WIP branch GitHub already holds. When A's host
  returns it rejoins as a replica (re-snapshotting if it diverged).
- **What is still lost:** transactions A had not yet replicated, and webhooks
  that reached nobody for longer than the proxy holds writes; those need a
  redelivery sweep (see §9, alternatives).
- **Static lease variant.** With `lease.type: static` there is no election: B
  is a hot copy that stays read-only. After an outage an operator redeploys B
  with `candidate: true` (and A must really be down — two candidates are two
  primaries). It trades automatic failover for no Consul dependency and no
  accidental split brain, and still cuts RPO from a day to seconds.

## 6. Changes to the infrastructure

| Area | Change |
|---|---|
| `Dockerfile` | Install `fuse3` (and `sqlite3` for debugging), copy `litefs` from the official image, `ENTRYPOINT litefs mount`. **Open decision: privileges.** The docs recommend running LiteFS as root, but `AGENTS.md` requires the container to run as UID 1000 and the agent's shell must never be root. Either LiteFS runs as root and the web/worker drop to `agent` (for example with `gosu`), or LiteFS mounts as non-root — the config has `fuse.allow-other` and the docs note non-root mounting needs `user_allow_other` in `/etc/fuse.conf`, but that has not been tried here. Needs a security review. |
| `litefs.yml` (new) | `fuse.dir: /litefs`; `data.dir` on the volume (e.g. `/data/litefs`); `lease` (`consul` or `static`), `candidate`, `advertise-url` built from `${FLY_ALLOC_ID}` / `${FLY_APP_NAME}`; `proxy` (`addr: ":8080"`, `target` = uvicorn's new port, `db: issue_agent.db`); `exec` to start honcho. |
| `fly.toml` | A second Machine in another region with its own volume (e.g. `flyctl machine clone --region`); `internal_port` stays the **proxy's** port; `fly consul attach` for the Consul lease; autostop stays **off**; health check `/readyz` as today. |
| `Procfile` | uvicorn moves to the port the LiteFS proxy targets. |
| CI / Deploy | `flyctl deploy` must update the two Machines one at a time; the smoke test already goes through the public hostname. |
| Runbooks | `operations.md`: failover and failback, and what each role looks like in `flyctl logs`; the snapshot-restore runbook stays as the second line of defense. |
| `AGENTS.md` | "Single Fly Machine" and "SQLite + persistent Fly Volume" are architecture constraints: update them, with this plan as the decision record. |

## 7. Changes to the code

None of this is written. File and function names are the current ones.

1. **Separate the database from `DATA_DIR`.** Today everything is rooted at
   `settings.data_dir` and `main.lifespan` builds `data_dir / "issue_agent.db"`.
   Add a `db_path` setting (alias `DB_PATH`, default `<data_dir>/issue_agent.db`,
   so nothing changes unless set) and use it in `main.py` and `worker.main`. The
   database goes to `/litefs/issue_agent.db`; workspaces, `events/`, audit logs
   and `tools/` stay on the local volume (they are per-Machine and do not need
   replicating).
2. **A role module** (e.g. `queue/role.py`): `Role.PRIMARY | REPLICA | UNKNOWN`
   from `/litefs/.primary`. With no LiteFS mount the role is `PRIMARY`, so local
   development and the current single-Machine production behave as today.
   `UNKNOWN` covers "no `.primary` file but cannot write" (a disconnected
   replica looks like a primary) — probe with a cheap write or LiteFS's API
   before trusting it.
3. **Only the primary writes and runs tasks.** `SQLiteStore.init()` (migrations
   and `PRAGMA journal_mode=WAL`) runs in **both** `main.lifespan` and
   `worker.main`; on a replica it would fail. Gate it, and the worker's startup
   sequence — `run_orphan_recovery`, `run_maintenance`, `reconcile_issue_work`
   — and the `fetch_next` / `run_retry_recovery` loop, on `Role.PRIMARY`. On
   `REPLICA → PRIMARY`, run init and `run_orphan_recovery` before the first
   `fetch_next`.
4. **Fencing.** The case that can duplicate work: A loses the lease while an
   agent is mid-task. The agent keeps calling GitHub while B, after
   `orphan_timeout_seconds`, re-queues and re-runs the same task. A demoted
   node's heartbeat write fails (on WAL the replica error is a "disk I/O
   error", per the docs); treat that as "abort the run" and check the role
   immediately before every external side effect in `executor.run_task` (git
   push, PR creation, comments). The agent subprocess must be stopped, not just
   abandoned (see `runner/guards.py`).
5. **Webhook handlers.** `webhook/routes.py` writes on POST (`mark_delivery_processed`,
   `insert_task…`) and the proxy ensures that happens on the primary. Verify
   that no `GET` handler writes (`/readyz`'s `SELECT 1` is fine) — the proxy
   rule is that `GET`s never write.
6. **Observability.** `/readyz` reports the role and the replication position
   (the `<db>-pos` file); the queue-depth metric and the audit log note the
   role. Audit logs and trajectories remain per Machine; after a failover the
   history is split between the two — acceptable, or ship the audit log to
   stdout.
7. **Settings and docs.** A feature flag (`litefs_enabled`, `litefs_dir`) so the
   whole thing stays off by default; `config.yaml.example` and
   `operations.md` follow.

**Tests.** Role detection with a fake `.primary`; worker gating and the
`REPLICA → PRIMARY` transition with an injected role provider; fencing (a
demoted node must not push or open a PR); a two-node `docker compose` with a
**static** lease for an end-to-end check; a kill-the-primary drill that records
the real failover time and the data lost.

## 8. Rollout in phases

0. **Prerequisites.** Rehearse the snapshot-restore runbook. Add an off-site
   backup (the LiteFS docs recommend one anyway). Settle the root/FUSE question
   in a throwaway Machine.
1. **Local spike.** Two nodes in `docker compose`, static lease, the full test
   suite against `/litefs`. Confirm: non-root mount, replica write errors, two
   processes (web and worker) on one mount, WAL behavior.
2. **LiteFS on the existing single Machine.** Import the database
   (`litefs import`), keep one Machine. This validates FUSE, privileges and
   overhead with no redundancy yet. Keep the snapshot as the way back.
3. **Add the replica in a second region with a static lease.** A hot copy with
   manual promotion. Drill the promotion.
4. **Optional: Consul lease for automatic failover**, with the fencing from §7.4
   in place. Drill by killing the primary.

Rollback at any phase: stop the replica, point `DB_PATH` back at a plain volume
file (export the database from the mount first — confirm the exact `litefs`
command in the docs, only `import` was read here).

## 9. Alternatives

| Option | RPO / RTO | Cost of the change | Note |
|---|---|---|---|
| **Stay as is + runbook** | up to 24 h / minutes of manual work | none | What exists now. |
| **Litestream (continuous backup to object storage) + redelivery sweep** | seconds / minutes (restore onto a new Machine) | small | No failover, but covers the single-host failure that happened. A scheduled job **outside Fly** can list the App's failed webhook deliveries and redeliver them (GitHub does not redeliver on its own); `processed_deliveries` keeps that idempotent. Whether a redelivery keeps the same delivery GUID is not stated in the docs consulted — confirm first. |
| **LiteFS, static lease** | seconds / minutes (manual promote) | medium | Hot copy in another region. |
| **LiteFS, Consul lease** | seconds / ~10–90 s | medium–high | Automatic; needs fencing. |
| **Postgres (shared, active-active)** | ~0 / ~0 | high | `fetch_next` becomes `SELECT … FOR UPDATE SKIP LOCKED`, deliveries `ON CONFLICT DO NOTHING`; rewrite of `queue/store.py` and its tests; **contradicts the `AGENTS.md` constraint** "SQLite + persistent Fly Volume, not Postgres". |
| **CRDT replication (cr-sqlite)** | eventual convergence, **no mutual exclusion** | high (changes the data model) | Keeps files in sync, but cannot stop two nodes claiming the same task. See [Why not a CRDT](#why-not-a-crdt-cr-sqlite). Not recommended. |
| **GitHub as the arbiter with separate databases** | — | — | Creating the work branch ref fails with 422 if it exists, which could act as a claim. The branch is also how work resumes between attempts, so the semantics get muddled. Not recommended. |

### Why not a CRDT (cr-sqlite)

[cr-sqlite](https://github.com/vlcn-io/cr-sqlite) is a loadable SQLite
extension ("multi-writer and CRDT support for SQLite"): every node writes
independently, and the nodes merge their changes later with last-write-wins per
column and eventual convergence. It is built for local-first applications —
many writers, often offline, whose data tolerates last-write-wins. It can keep
SQLite files in sync, but the outage in §1 is not what it solves, and §3 is
exactly what it would reintroduce: the problem there was never that two
databases differ, it is that **two nodes act on the same state at the same
time**. A CRDT merges after the fact; it cannot prevent that. Convergence is not
coordination.

What our schema would run into (constraints from the
[cr-sqlite constraints page](https://vlcn.io/docs/cr-sqlite/constraints) and
its README):

| Our code | With a CRDT |
|---|---|
| `fetch_next` claims a task with `BEGIN IMMEDIATE` | Both nodes claim the same `pending` task and both run the agent; the databases converge afterwards, the duplicate PRs and comments are already out. |
| `idx_tasks_unique_pending` (partial unique index) | UNIQUE constraints other than the primary key are not allowed, so the guard against two tasks for one issue has to go, and an application-level replacement cannot be globally atomic. |
| `processed_deliveries` dedupe | Two nodes insert the same `delivery_id` concurrently and both treat it as new. |
| `task_events.task_id REFERENCES tasks(id)` | Checked foreign keys are not allowed on replicated tables. |
| `task_events.id` is `AUTOINCREMENT` | Independent counters hand out the same id on both nodes, which then merge as one row. It would need UUID primary keys. (A consequence of the design; the pages read do not cover autoincrement.) |
| `daily_budget`, `issue_work.attempts` (read-modify-write counters) | Last-write-wins overwrites a concurrent increment, so spend and attempts are undercounted. The README lists counter CRDTs as still being implemented. |

What would also have to be built: the transport (the extension only exposes the
changes through the `crsql_changes` virtual table; the networking is the
application's), the native `linux-x64` binary shipped in the image and loaded on
**every** connection (with `crsql_finalize()` before closing), confirmation that
the image's Python `sqlite3` allows loading extensions (not checked), schema
changes through `crsql_begin_alter` / `crsql_commit_alter`, and the redesign
above. The result would be a different database with different guarantees, not
the one this code was written against. LiteFS replicates the file physically
with a single writer, so every SQLite guarantee — transactions, the unique
index, the atomic claim — keeps working unchanged.

**Maintenance is not what separates them.** As of 2026-10-17: cr-sqlite's
latest release is v0.16.3 (2024-01-17); its recent commits on `main` (August
2026) touch build tooling — the Makefile, the publish workflow and the
iOS/Android/Windows/macOS loadable builds — no other branch has a commit later
than June 2024, and its README advises building from a release tag because
`main` "may not be 100% stable". LiteFS's `main` has had no commit since
2025-04-22 (v0.5.14), with two unmerged branches by one contributor
(`saleemrashid`) dated April and May 2026 (FUSE and checksum work). Both move
slowly; commit recency says little about either, so the choice rests on fit.

Where cr-sqlite would fit: a many-writer, offline-tolerant application whose
data merges by last-write-wins. At most it could carry an append-only table such
as the audit trail, which is not worth a mixed design.

## 10. Risks and open questions

- **Pre-1.0, no support.** The docs say so. The failure mode of the thing that
  protects the data would be the data.
- **Slow maintenance on every option.** LiteFS's `main` is 18 months idle and
  cr-sqlite's last release is from January 2024 (§9). Whichever is chosen, plan
  to own it: pin the version, keep the backups, and budget for a fork or a
  fallback to the single-Machine setup.
- **Asynchronous replication.** There is a window of lost transactions on a
  crash; the docs give no figures. Measure it in the drill.
- **Root.** See §6. This is the main conflict with an existing constraint.
- **Consul on Fly.** The getting-started guide uses `fly consul attach`. Confirm
  it is still offered and what it costs before depending on it.
- **`.primary` ambiguity.** A disconnected replica looks like a primary (§7.2).
- **Webhook timing.** GitHub marks a delivery failed after 10 s. Failover plus
  `primary-redirect-timeout` can exceed that; the redelivery sweep covers it.
- **Autostop.** Must stay off with LiteFS (docs). It is off today.
- **Cost.** A second Machine of the same size roughly doubles the compute bill.
  On Fly's price list a `shared-cpu-1x` is US$2.19/month at 256 MB plus
  US$6.00 per extra GB (≈ US$6.69 at 1 GB in `iad`); the calculator's multiplier
  for `gru` is ~1.62×, so a second Machine in a cheaper region costs less than
  the first. Volume pricing was not checked.
- **Two processes on one mount.** Web and worker both open the database today;
  confirm that is fine through FUSE.

## 11. Decision gate

Adopt this only if, after the cheaper options (the runbook, then Litestream and
the redelivery sweep), the cost of an outage still outweighs owning a pre-1.0
component and a privileged container. Revisit after the restore drill: its
measured RTO and the data it actually loses are the baseline this plan has to
beat.

## References

- [LiteFS](https://docs.fly.io/litefs) ·
  [How it works](https://docs.fly.io/litefs/how-it-works) ·
  [Configuration](https://docs.fly.io/litefs/config) ·
  [Proxy](https://docs.fly.io/litefs/proxy) ·
  [Primary detection](https://docs.fly.io/litefs/primary) ·
  [Getting started on Fly](https://docs.fly.io/litefs/getting-started-fly)
- [Fly volumes](https://docs.fly.io/volumes/overview/) (one Machine per volume,
  no replication, daily snapshots) ·
  [Load balancing](https://docs.fly.io/reference/load-balancing/)
- [cr-sqlite](https://github.com/vlcn-io/cr-sqlite) ·
  [its constraints](https://vlcn.io/docs/cr-sqlite/constraints)
- [GitHub: handling failed webhook deliveries](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries)
- Code: `src/issue_agent/queue/store.py`, `queue/schema.sql`,
  `webhook/routes.py`, `worker.py`, `main.py`, `runner/executor.py`
