# Chronos Implementation Handoff

**READ THIS FILE FIRST BEFORE MAKING ANY CHANGES.**

This document is self-contained. A fresh Claude Code session with no memory
of prior conversations should be able to read only this file and correctly
continue the Chronos implementation.

---

## 1. What Chronos Is

Chronos is a distributed task-orchestration system: workflows composed of
tasks with dependency edges (a DAG), executed by a fleet of workers, with
PostgreSQL as the durable source of truth for task state.

### High-level architecture (target, not all built yet)

```
Client
  |
  v
API                          (NOT built)
  |
  v
PostgreSQL
  |-- task state              (Phase 1 -- COMPLETE)
  \-- outbox_events           (Phase 2 -- COMPLETE)
          |
          v
     Outbox Relay             (Phase 3 -- COMPLETE, verified against real RabbitMQ)
          |
          v
      RabbitMQ                (Phase 3 -- COMPLETE, verified against real RabbitMQ 4.3.5)
          |
          v
       Worker                 (Phase 4 -- COMPLETE for claim/ack/execute/report; verified
          |                    against real RabbitMQ. Heartbeats intentionally deferred.)
          v
   PostgreSQL claim_task_by_id()
          |
          v
       execute
```

**UPDATE (see section 25 for full detail, supersedes section 24 below):
Phase 1-7 are all COMPLETE. The full test suite (289/289) passed with
PostgreSQL, RabbitMQ, AND Redis all live simultaneously** -- no mocks for
any of the three real dependencies in that run. The reaper, fallback
poller, worker lease heartbeat, and the Redis per-task-type admission
limiter are all built and verified. Only API/scheduler/frontend remain
unbuilt (never scoped into any phase yet).

### Why PostgreSQL exists

PostgreSQL is the single source of truth for task state. It is not a cache,
not an index, not eventually-consistent storage sitting behind some other
authority. Every state transition is committed there first, under a row
lock or an atomic conditional UPDATE, before anything else (an outbox
event, eventually a RabbitMQ message) is allowed to reference it. Nothing
downstream (RabbitMQ, a worker, Redis) is ever treated as authoritative.

### Architectural separation of concerns

| Component | Responsibility |
|---|---|
| `core/transitions.py` | State-machine correctness (frozen, pure functions) |
| PostgreSQL | Durable truth + concurrency control (row locks, atomic claim) |
| Outbox (`outbox_events` table) | Durable, transactionally-consistent notification record |
| RabbitMQ (Phase 3, COMPLETE -- see section 24) | Low-latency dispatch *hint* only, never authoritative |
| Worker (Phase 4, COMPLETE -- see section 24) | Executes tasks; must re-verify state via PostgreSQL claim |
| Lease/Reaper + Fallback poller (Phase 5, COMPLETE -- see section 25) | Crash recovery when a worker dies mid-execution; claim_task() fallback when the broker is unavailable |
| Lease heartbeat (Phase 6, COMPLETE -- see section 25) | Background renewal of a claimed task's PostgreSQL lease during execution |
| Redis (Phase 7, COMPLETE -- see section 25) | Optional, fail-open, per-task-type admission control |

This separation is deliberate and must not be collapsed. In particular:
RabbitMQ never carries authoritative state, and a worker must never trust
a message body as ground truth -- it always re-claims the task in
PostgreSQL before doing anything.

---

## 2. The Frozen State Machine

**`core/transitions.py` is the single authoritative source of task-state
correctness.** It is a pure, dependency-free module: no database, no HTTP,
no scheduler, no Redis, no migrations knowledge. Every transition function
takes an immutable `TaskInstance` and returns a `TransitionResult`
(`task`, `applied`, optional `reason`) -- nothing is mutated in place.

### Important transitions

- **`create_task(*, has_dependencies, max_attempts=1) -> TaskInstance`**
  Zero dependencies -> `READY`. One or more dependencies -> `PENDING`.
- **`mark_ready(task, *, dependencies_satisfied) -> TransitionResult`**
  `PENDING` -> `READY` once dependencies are satisfied. Already-`READY` is
  a safe no-op. Any other source state raises `TransitionError` (caller
  bug).
- **`claim(task, *, worker_id, now, lease_duration) -> TransitionResult`**
  `READY` -> `RUNNING`. Increments `attempt_count` exactly once, exactly
  here. Invalid source state (lost a claim race) is `applied=False`, not
  an exception.
- **`report_success(task, *, worker_id, now) -> TransitionResult`**
  `RUNNING` -> `SUCCEEDED`, only from the current lease holder. Stale/
  non-owning reports are safely ignored (`applied=False`).
- **`report_failure(task, *, worker_id, now) -> TransitionResult`**
  `RUNNING` -> `READY` (retry) or `RUNNING` -> `FAILED_TERMINAL`
  (attempts exhausted), decided by the shared `_resolve_after_failure`
  helper. Same stale-report handling as `report_success`.
- **`expire_lease(task, *, now) -> TransitionResult`**
  Reaper's entry point (reaper itself not built). Same retry-vs-terminal
  decision as `report_failure`, but driven by lease expiry, not a worker
  report. Raises `TransitionError` if the task isn't `RUNNING`, has no
  lease, or the lease hasn't actually expired.
- **`skip(task) -> TransitionResult`**
  `PENDING` -> `SKIPPED` via the downstream dependency-failure cascade.
  Idempotent (already-`SKIPPED` is a no-op). Any other state raises
  `TransitionError`.

### Explicit rule

**DO NOT MODIFY `core/transitions.py`** unless a future task *explicitly*
requires revisiting the frozen state-machine specification. Phase 1 and
Phase 2 deliberately wrap and call into it rather than duplicating any of
its transition-legality rules in SQL or persistence-layer Python.

### `TaskInstance` fields

```python
@dataclass(frozen=True)
class TaskInstance:
    state: TaskState
    attempt_count: int = 0
    max_attempts: int = 1
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
```

These five fields are the *entire* state-machine-relevant surface.
`TaskInstance` never gains a database identity field. Identity/metadata
that belongs to persistence, not the core state machine, lives instead on
`db/models.py`'s `TaskRecord`: `id`, `workflow_id`, `name`, `task_type`,
`payload`, `created_at`, `updated_at`, wrapping an embedded
`TaskInstance`.

---

## 3. Phase 1 — What Was Implemented (COMPLETE)

- **PostgreSQL schema** (`db/migrations/0001_init.sql`): `task_state` enum,
  `workflows`, `tasks`, `task_dependencies` tables, plus indexes.
- **Migrations** (`db/migrate.py`): a minimal, hand-rolled runner (not
  Alembic). Applies every `*.sql` file in `db/migrations/` in filename
  order, tracked in a `schema_migrations` table, each migration + its
  bookkeeping row committed atomically. Re-running is a no-op. The runner
  itself bootstraps `schema_migrations` (not owned by any versioned
  migration file, since the runner needs it to exist before it can check
  what's applied).
- **Connection/session handling** (`db/connection.py`): psycopg 3 +
  `psycopg_pool.ConnectionPool`. Every connection gets `row_factory =
  dict_row` and `SET TIME ZONE 'UTC'` (required so `timestamptz` reads
  come back exactly UTC-offset-zero, matching `core/transitions.py`'s
  strict aware-UTC contract).
- **Models** (`db/models.py`): `TaskRecord` (DB-side view: identity +
  embedded `TaskInstance`), `row_to_task_instance`, `row_to_task_record`,
  `task_instance_update_params`.
- **SQL query layer** (`db/queries.py`): every SQL statement as a named
  constant. Nothing in `db/persistence.py` embeds raw SQL in a function
  body.
- **Persistence layer** (`db/persistence.py`): `create_workflow`,
  `create_task`, `claim_task`, `report_task_success`,
  `report_task_failure`, `expire_lease`, `mark_ready`, `skip`, plus
  internal `_propagate_ready` and `_propagate_skip_cascade`.
- **Atomic `SKIP LOCKED` claiming**: `claim_task` does *not* go through
  `core.transitions.claim` (would require a read-then-write round trip,
  reintroducing the race `FOR UPDATE SKIP LOCKED` exists to avoid).
  Instead a single CTE + UPDATE statement
  (`queries.CLAIM_READY_TASK`) selects one `READY` row with
  `FOR UPDATE SKIP LOCKED` and claims it in the same statement/snapshot,
  encoding the identical computation `core.transitions.claim` would have
  produced, so the two can never drift as long as `core/transitions.py`
  stays frozen.
- **Dependency handling**: `task_dependencies(task_id, depends_on_task_id)`
  with a `no_self_dependency` check constraint.
- **PENDING -> READY propagation**: `_propagate_ready` runs inside the
  same transaction as a `report_task_success` completion; locks every
  `PENDING` dependent (`ORDER BY t.id ASC`, mandatory for deadlock
  avoidance across overlapping fan-out sets), checks
  `ALL_DEPENDENCIES_SUCCEEDED`, and calls `transitions.mark_ready` for
  each. Does not recurse further.
- **SKIPPED cascade**: `_propagate_skip_cascade` -- an explicit worklist
  (never naive recursion) with a `visited` set, so a diamond-shaped DAG is
  walked at most once per node even when reachable via two failed paths.
- **Pessimistic locking**: every single-task transition
  (`report_task_success`, `report_task_failure`, `expire_lease`,
  `mark_ready`, `skip`) does `SELECT ... FOR UPDATE` before deciding
  anything.
- **Indexes/constraints**: `idx_tasks_ready_claim` (partial, `WHERE state
  = 'READY'`), `idx_tasks_running_lease` (partial, `WHERE state =
  'RUNNING'`), `idx_tasks_workflow_id`, `idx_tasks_state`,
  `idx_task_deps_depends_on`; check constraints `lease_pair_consistent`,
  `lease_requires_running`, `attempt_bounds`, `max_attempts_positive`,
  `no_self_dependency` -- these mirror `TaskInstance.__post_init__`'s own
  invariants in SQL, so a hand-written migration or bad hotfix query can't
  corrupt a row into a state the frozen module would never produce.
- **Real PostgreSQL concurrency testing**: every concurrency test uses
  genuinely separate psycopg connections (never in-process threads sharing
  one connection), against a real local PostgreSQL instance.

### Important PostgreSQL correctness rules

- PostgreSQL is the source of truth for task state -- always.
- Task claiming is conditional and atomic: `CLAIM_READY_TASK`'s CTE +
  `UPDATE` runs as one statement in one snapshot; there is no gap between
  "pick a row" and "claim it" for a second transaction to land in.
- Concurrent workers cannot both claim the same `READY` task --
  `FOR UPDATE SKIP LOCKED` guarantees two concurrent claimants always lock
  different rows.
- Deterministic lock ordering (`ORDER BY t.id ASC` before any multi-row
  `FOR UPDATE`) is used everywhere fan-out locking happens (both the
  `PENDING`->`READY` propagation pass and the `SKIPPED` cascade), to avoid
  deadlocks when two concurrent completions' fan-out sets overlap.

---

## 4. Phase 1 Verification

- PostgreSQL 18.6 running locally, connection verified directly via
  psycopg against `CHRONOS_TEST_DATABASE_URL`.
- **99 Phase 1 tests passed, 0 failed, 0 skipped.**
- Real PostgreSQL was used throughout -- no mocks for anything
  transactional or concurrent.
- Real multi-connection concurrency tests were executed
  (`tests/test_claiming_concurrency.py`): no-double-claim under
  contention, `SKIP LOCKED` non-blocking behavior, rollback releasing
  locks, attempt-count-never-exceeds-max under contention, DB-level stale
  report fencing, concurrent fan-in completion promoting a downstream task
  exactly once, and a general invariant fuzz pass.
- **One bug was found and fixed**: `test_migration_applies_cleanly_and_is_idempotent`
  in `tests/test_db_schema.py` was order-dependent. It asserted that
  calling `apply_migrations()` on the shared session-scoped test database
  would be the call that first applies `"0001_init"`. But pytest's default
  collection order is alphabetical by filename, so
  `tests/test_claiming_concurrency.py` runs first and, via its `db_dsn` ->
  `db` -> `pg_pool` fixture chain, already triggers `apply_migrations` as
  a side effect before this test runs -- leaving the observed `first_run`
  empty. **This was a test/fixture ordering issue, not an
  application/database bug.** It was fixed by rewriting the assertion to
  check the real invariants (`"0001_init"` ends up in
  `applied_versions(conn)`, and a subsequent `apply_migrations()` call
  returns `[]`) instead of asserting *this particular call* is the one
  that applies it. No application code was touched.
- The Phase 1 suite was ultimately **fully green**.

---

## 5. ID Decision — Very Important

Final Chronos ID strategy, reconciled and locked in:

- `workflows.id` = `BIGINT GENERATED ALWAYS AS IDENTITY`
- `tasks.id` = `BIGINT GENERATED ALWAYS AS IDENTITY`
- `outbox_events.id` = `BIGINT GENERATED ALWAYS AS IDENTITY`
- `task_dependencies` uses `BIGINT` foreign keys (`task_id`,
  `depends_on_task_id`)
- All relevant FK columns (`tasks.workflow_id`, `outbox_events.task_id`,
  `outbox_events.workflow_id`) use `BIGINT`

An earlier specification used UUIDs for identity. This was **explicitly
rejected and reconciled to BIGINT** before any Phase 1/2 implementation.
Reasoning:

- Smaller keys.
- Better index locality.
- Cheaper joins, particularly relevant for FK-heavy tables like this one
  (`tasks` -> `workflows`, `task_dependencies` -> `tasks` x2,
  `outbox_events` -> `tasks` + `workflows`).
- UUID's external-opacity property (useful for public-facing IDs that
  shouldn't leak sequence/cardinality information) was not currently
  required anywhere in this design.
- If future API idempotency needs a client-supplied opaque token, that
  should be a separate idempotency key column/table, not a change to any
  primary key type.

**Do NOT change this back to UUID in any future implementation unless
explicitly instructed.**

---

## 6. State Column Decision

The canonical task state column is **`state`**, not `status`. This follows
directly from the frozen `TaskInstance`/`TaskState` terminology in
`core/transitions.py`. The `tasks.state` column is `task_state` (a native
Postgres enum: `PENDING`, `READY`, `RUNNING`, `SUCCEEDED`,
`FAILED_TERMINAL`, `SKIPPED`).

**Do not introduce a `status` column anywhere.**

---

## 7. Phase 2 — Transactional Outbox (COMPLETE)

Exact schema implemented, in `db/migrations/0002_outbox.sql`:

```sql
CREATE TABLE outbox_events (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id       BIGINT NOT NULL REFERENCES tasks(id),
    workflow_id   BIGINT NOT NULL REFERENCES workflows(id),
    event_type    TEXT NOT NULL DEFAULT 'TASK_READY',
    payload       JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at  TIMESTAMPTZ
);

CREATE INDEX idx_outbox_unpublished
ON outbox_events (created_at)
WHERE published_at IS NULL;
```

The canonical payload shape for every `TASK_READY` event:

```json
{
    "task_id": <BIGINT>,
    "task_type": "<TEXT>",
    "workflow_id": <BIGINT>
}
```

This is a dispatch hint, never authoritative task state -- it deliberately
excludes `attempt_count`, lease fields, and the task's own `payload`
column.

Explicitly, Phase 2 does **not** include:
- UUIDs (BIGINT identity throughout, per section 5)
- relay claim/lease metadata columns on `outbox_events`
- a message-deduplication table
- any extra outbox-specific state machine
- any RabbitMQ implementation

---

## 8. The Central Outbox Invariant

**EVERY successful task transition that produces READY must create
exactly one TASK_READY outbox event in the SAME PostgreSQL transaction.**

The four main READY-producing paths, and where each is implemented in
`db/persistence.py`:

1. **Zero-dependency task creation** -- `create_task`: if
   `instance.state == TaskState.READY` (no `depends_on_ids`), insert the
   outbox row using the just-inserted task row, in the same transaction as
   `INSERT_TASK`.
2. **Dependency propagation (`PENDING` -> `READY`)** -- `_propagate_ready`:
   for each dependent whose `transitions.mark_ready(...)` call returns
   `applied=True`, write the transition and insert exactly one outbox row,
   all inside the caller's already-open transaction/cursor (the completion
   that triggered the propagation pass).
3. **Retry via `report_failure`** -- `report_task_failure`: if the
   resulting state is `READY` (attempts remain), insert the outbox row
   right after `_write_transition`, same transaction.
4. **Retry via `expire_lease`** -- `expire_lease` (persistence layer,
   reaper's future entry point): same shape as (3).

Also documented: the **standalone `persistence.mark_ready(...)` entry
point** (exposed directly, in addition to being used internally by
`_propagate_ready`) uses the exact same READY transition semantics --
`core.transitions.mark_ready`'s only legal `applied=True` outcome is
`PENDING` -> `READY` -- so it unconditionally inserts one outbox event
whenever it applies. This is not one of the "four paths" as originally
enumerated in the architecture brief, but the central invariant in this
section requires it, since it is the same underlying transition.

### Why the outbox insert must use the same transaction/cursor

If the task-state write and the outbox write were two separate
commits, a crash or error between them would leave either an orphaned
`READY` task with no dispatch event (task silently stuck) or an orphaned
outbox event pointing at a task that was never actually transitioned (a
dispatch hint with a lie behind it) -- the classic dual-write problem.
Using the same `with conn.transaction():` block and the same cursor for
both statements means one exception anywhere in that block rolls both
back together via psycopg's transaction context manager. This was
verified directly: a forced error after both inserts rolls back both; a
forced FK violation on the outbox insert itself rolls back the task
insert too.

---

## 9. Non-READY Transitions

These do **NOT** produce outbox events (all verified by dedicated tests):

- `READY` -> `RUNNING` (`claim_task`)
- `RUNNING` -> `SUCCEEDED` (`report_task_success`, for the succeeding task
  itself -- a dependent it unblocks may still get its own event per
  section 8)
- `RUNNING` -> `FAILED_TERMINAL` (`report_task_failure` /
  `expire_lease`, terminal branch)
- `PENDING` -> `SKIPPED` (`skip`)
- The `SKIPPED` cascade (`_propagate_skip_cascade`)
- Any `applied=False` / no-op transition (stale worker report, lost claim
  race, already-`READY` no-op, etc.)

---

## 10. Phase 2 Implementation Files

**Created:**
- `db/migrations/0002_outbox.sql`
- `tests/test_outbox.py`

**Modified:**
- `db/queries.py` -- added `INSERT_OUTBOX_EVENT`,
  `SELECT_UNPUBLISHED_OUTBOX_EVENTS` (the latter documents the future
  relay's intended read shape; not called by anything yet)
- `db/persistence.py` -- added `_insert_ready_outbox_event` helper and
  call sites at all READY-producing paths listed in section 8
- `db/migrations/0001_init.sql` -- **comment-only** change: the old "must
  not be implemented yet" wording for `outbox_events` was updated to say
  it is created by `0002_outbox.sql`. No schema change.

**Untouched (do not claim these were modified -- they were not):**
- `core/transitions.py`
- `db/models.py`
- `db/migrate.py`
- `tests/test_db_schema.py`
- `tests/test_persistence.py`
- `tests/test_claiming_concurrency.py`
- `tests/test_transitions.py`

---

## 11. Phase 2 Testing

```
pytest tests/test_outbox.py -v
-> 27 passed, 0 failed, 0 skipped
```

```
pytest
-> 126 passed, 0 failed, 0 skipped
-> ~6.7 seconds
-> no warnings (re-verified under -W error::DeprecationWarning)
```

126 = 99 existing Phase 1 tests (unchanged in behavior) + 27 new Phase 2
tests.

Phase 2 tests (`tests/test_outbox.py`) cover:
- **schema**: table exists, `id`/`task_id`/`workflow_id` are BIGINT
  (identity / FK), `event_type` NOT NULL default `TASK_READY`, `payload`
  JSONB NOT NULL, `created_at` NOT NULL / `published_at` nullable,
  `idx_outbox_unpublished` exists and is partial, `0002_outbox.sql`
  applies cleanly after `0001_init.sql` and is idempotent
- **zero-dependency creation**: exactly one outbox row with the correct
  payload; dependency-having creation produces zero rows while `PENDING`
- **dependency propagation**: exactly one row per dependent that actually
  transitions, no premature event before all deps are satisfied, and a
  **real concurrent fan-in** test (two threads, two separate connections,
  completing both parents of a 2-in fan-in concurrently) proving no
  duplicate/missing event
- **retry via `report_failure`**: one event on retry, zero additional
  events on terminal failure
- **retry via `expire_lease`**: one event on retry, zero additional events
  on terminal lease expiry
- **non-READY transitions**: claim, success (for the task itself), and the
  full skip cascade all produce zero events
- **transaction rollback**: a forced error after both a task insert and an
  outbox insert leaves neither row behind; a forced FK violation on the
  outbox insert alone rolls back the task insert too; an `applied=False`
  transition creates no event
- **concurrency/idempotency**: hammering retries from 5 concurrent
  connections produces exactly `attempts - 1` retry events (no dupes, none
  dropped); 20 tasks claimed concurrently by 5 workers each still have
  exactly one creation-time outbox row apiece

---

## 12. Phase 2 Verification Details

- Real PostgreSQL was used for every Phase 2 test -- no mocks.
- Transactional and concurrency behavior was not mocked anywhere.
- Outbox atomicity (same-transaction task-write + outbox-write) was
  explicitly tested, not just asserted in comments.
- Rollback behavior was tested from both directions: task-write failure
  rolling back a pending outbox insert, and outbox-insert failure (FK
  violation) rolling back the task write.
- `core/transitions.py` was not edited during Phase 2 -- confirmed by
  never opening it with an editing tool, only `Read`.
- SHA-256 fingerprint of `core/transitions.py`, recorded at the end of
  today's session (2026-08-19) as an integrity checkpoint for the next
  session to diff against:
  `3674492749fe3b4b04323f82be0b5e56f8a4a83cccef6aa7fbb5d7f8f207d20a`

---

## 13. What Has NOT Been Implemented

**UPDATED as of the Phase 5/6/7 completion session -- see section 25 for
full detail (supersedes this section's older Phase 3/4-era wording,
preserved in git history / earlier revisions only).** RabbitMQ, the
outbox relay, the worker consumer, the reaper, the fallback poller, the
worker lease heartbeat, and the Redis per-task-type admission limiter are
ALL now COMPLETE and verified against real infrastructure (section 25).
What remains genuinely unbuilt:

- API
- Scheduler
- Frontend
- Exactly-once delivery of any kind
- Generic message deduplication
- A distributed worker registry (worker_id is a stable, externally
  configured value -- see `mq.config.WorkerConfig` /
  `worker.config.FallbackPollerConfig` -- not a registry)
- `expected_task_type` predicate on `claim_task_by_id` (considered during
  Phase 4, deliberately not added -- no concrete correctness gap required it)
- Redis slot renewal from the lease heartbeat (considered during Phase 7,
  deliberately not added -- the Redis slot's TTL is a fixed
  admission-control approximation, not renewed the way the PostgreSQL
  lease is; see section 25.3)
- Worker-side DLQ *processing* (the DLQ itself is declared and messages
  are correctly routed to it by Phase 3's topology + Phase 4's reject
  behavior; nothing consumes/inspects/replays it yet)
- Redis limiter admission control for the fallback poller (deliberately
  excluded -- `worker/fallback_poller.py` claims via the untargeted
  `claim_task()` and doesn't know a task's type until after claiming;
  gating it would mean guessing the type or claiming-then-releasing,
  defeating the point -- see section 25.3)

**Do not accidentally implement any of the items above just because
sections 14-19 below describe the intended future architecture.** Those
sections were written before Phase 3 existed at all and describe the
*original plan*; sections 24 and 25 document what was actually built and
how it differs from that plan in small, deliberate ways. Do not build
ahead of the approved phase order (section 21, updated) -- there is no
approved "Phase 8" yet.

---

## 14. Future Phase 3 Architecture (NOT implemented)

```
Client
  |
  v
API
  |
  v
PostgreSQL
  |-- task state
  \-- outbox_events
          |
          v
     Outbox Relay
          |
          v
      RabbitMQ
          |
          v
       Worker
          |
          v
   PostgreSQL claim()
          |
          v
       execute
```

**Important rule: PostgreSQL remains authoritative.** RabbitMQ only ever
carries a "go try claiming this task" dispatch hint. A worker must never
trust a message body as authoritative task state -- it always performs its
own targeted, idempotent PostgreSQL claim before doing anything.

---

## 15. Future Outbox Relay (NOT implemented)

Intended Phase 3 relay behavior:

- Poll unpublished outbox rows (`SELECT_UNPUBLISHED_OUTBOX_EVENTS` in
  `db/queries.py` already documents this read shape: `WHERE published_at
  IS NULL ORDER BY created_at LIMIT :batch_size`)
- Publish each to RabbitMQ
- Use publisher confirms
- Only after confirmation, set `published_at = now()`
- Retry with backoff when the broker is unavailable
- Duplicate delivery is acceptable
- **At-least-once delivery is intentional**, not a bug to fix

Do NOT implement this now.

---

## 16. Future RabbitMQ Design (NOT implemented)

Approved intended design:

- **Exchange**: `chronos.tasks`
- **Type**: topic
- **Routing key**: `task.<task_type>`
- Durable queues per task type

Message payload:

```json
{
    "event_id": ...,
    "task_id": ...,
    "workflow_id": ...,
    "task_type": ...
}
```

The message is a pointer/hint, never authoritative state.

- Persistent delivery
- Manual acknowledgement
- Small consumer prefetch
- Dead-letter exchange for poison messages

Do NOT implement now.

---

## 17. Future Worker Semantics (NOT implemented)

1. Consume dispatch hint
2. Optionally acquire a Redis slot
3. Perform a targeted PostgreSQL `claim()`
4. **ACK immediately after the claim attempt resolves** -- the worker does
   NOT wait until execution finishes before ACKing
5. If claim fails (task already claimed / no longer `READY`), do nothing
6. If claim succeeds, execute the task
7. Heartbeat to PostgreSQL during execution
8. Report `report_success`/`report_failure` directly to PostgreSQL
9. Release the Redis slot in a `finally` block

---

## 18. Future Failure Model (NOT implemented)

Intended separation of failure-handling responsibility:

- **RabbitMQ handles**: message not consumed; worker crash before
  claim/ack.
- **PostgreSQL lease/reaper handles**: worker crash *after* a successful
  claim and ACK, during execution (lease expiry -> `expire_lease`).
- **Fallback polling handles**: broker unavailable entirely.
- **Redis handles**: optional global task-type admission control only --
  never correctness.

This separation is an important architectural invariant: each failure
mode has exactly one owner, and no two components are ever relied on to
catch the same failure.

---

## 19. Future Redis Limiter (NOT implemented)

Documented only, not built:

- Optional per-task-type global concurrency limit
- Redis ZSET as the semaphore structure
- Member: `worker_id:task_id`
- Score: expiry timestamp
- Atomic Lua script for acquire
- `ZREM` for release
- TTL-based crash recovery (a dead worker's slot ages out)
- **Fail-open by default** -- if Redis is unavailable, admission control
  is skipped rather than blocking all task dispatch

---

## 20. Architectural Principle

| Component | Responsibility |
|---|---|
| `core/transitions.py` | State-machine correctness |
| PostgreSQL | Durable truth + concurrency |
| Outbox | Durable notification record |
| RabbitMQ | Low-latency dispatch hint |
| Worker | Execution |
| Lease/Reaper | Crash recovery |
| Redis | Optional admission control |

---

## 21. Next Implementation Order

```
Phase 1 -- PostgreSQL persistence                -> COMPLETE
Phase 2 -- Transactional outbox                  -> COMPLETE
Phase 3 -- RabbitMQ topology + outbox relay       -> COMPLETE (verified, real RabbitMQ)
Phase 4 -- RabbitMQ worker consumption            -> COMPLETE (verified, real RabbitMQ)
Phase 5 -- Reaper + fallback polling              -> COMPLETE (verified, real PostgreSQL)
Phase 6 -- Worker lease heartbeat                 -> COMPLETE (verified, real PostgreSQL)
Phase 7 -- Redis concurrency limiter              -> COMPLETE (verified, real Redis)
Phase 8 -- (not yet defined/approved)             -> NOT STARTED
```

**Numbering note:** Phase 6 (heartbeat) and Phase 7 (Redis limiter) were
not both present in the original phase table at the top of this document
-- that original table had only "Phase 5 -- Reaper/fallback" and
"Phase 6 -- Redis." Heartbeat was carved out of Phase 4's deferred scope
and inserted as its own phase once the reaper (its prerequisite) existed;
Redis shifted from Phase 6 to Phase 7 to make room for it. This table is
the authoritative, current numbering -- see section 25 for the full
verified record.

**Do not skip ahead.** See section 24 for Phase 3/4 and section 25 for
Phase 5/6/7.

---

## 22. Next Session Instruction

**READ THIS FILE FIRST BEFORE MAKING ANY CHANGES.**
**Section 22 below is preserved as historical record of the Phase 3
handoff. It has been superseded -- Phase 3 and Phase 4 are now COMPLETE
(section 24). The next implementation task is Phase 5: reaper /
lease-expiry recovery + fallback polling.** Before writing code, inspect
the current repository and verify the Phase 1-4 implementation still
matches this handoff (including section 24). Do not modify
`core/transitions.py`. Do not redesign Phase 1-4. Do not implement Redis
or the admission limiter until Phase 6.

Also, before implementing anything:
- Run the existing test suite first (with `CHRONOS_TEST_DATABASE_URL` and
  `CHRONOS_TEST_RABBITMQ_URL` both set, if you want the full real-infra
  run rather than the partial-skip run -- see section 24).
- Confirm 217 tests remain green (or 212 passed / 5 skipped if only
  Postgres is available, per section 24's baseline notes).
- Inspect `core/transitions.py::expire_lease` (already implemented,
  frozen, unused by any running process yet) and `db/persistence.py`'s
  `expire_lease` wrapper (already implemented in Phase 1, also unused by
  any running process yet) before building the reaper loop around them.
- Then propose/implement Phase 5 only.

*(Original Phase 3 handoff text, preserved below for history:)* The next
implementation task is **Phase 3: RabbitMQ topology + outbox relay**.
Before writing code, inspect the current repository and verify the Phase
1/2 implementation still matches this handoff. Do not modify
`core/transitions.py`. Do not redesign Phase 1/2. Do not implement worker
consumption, Redis, reaper, or fallback polling until their designated
phases.

Also, before implementing anything:
- Run the existing test suite first.
- Inspect the Phase 2 implementation (`db/migrations/0002_outbox.sql`,
  `db/queries.py`, `db/persistence.py`, `tests/test_outbox.py`).
- Confirm 126 tests remain green.
- Then propose/implement Phase 3 only.

---

## 23. Final Status (superseded -- see section 24 for current status)

```
CHRONOS CHECKPOINT -- END OF THAT SESSION (Phase 1/2 only)

Phase 1:              COMPLETE
Phase 2:               COMPLETE
Tests:                 126 passed, 0 failed, 0 skipped
PostgreSQL:             VERIFIED
core/transitions.py:    UNCHANGED
RabbitMQ:               NOT STARTED
Worker:                 NOT STARTED
Reaper:                 NOT STARTED
Redis:                  NOT STARTED
```

This section is preserved as a historical checkpoint. **It is no longer
current.** See section 24 for the up-to-date, fully-verified Phase 1-4
checkpoint.

---

## 24. Phase 3 & Phase 4 Completion — Verified Integrated Baseline

**READ THIS SECTION FIRST for the current state of the repository.**
Sections 1-23 above are preserved for historical context (they document
the plan and the Phase 1/2-only checkpoint); this section is the
authoritative record of what Phase 3 and Phase 4 actually built, the two
real bugs found and fixed along the way, and the final verified baseline.

### 24.1 Phase 3 — RabbitMQ topology + outbox relay: COMPLETE

Implemented exactly per the approved design (handoff sections 14-16, with
no deviation):

- **Topology** (`mq/topology.py`): durable topic exchange `chronos.tasks`,
  durable topic DLX `chronos.tasks.dlx`, one durable queue per task type
  (`chronos.tasks.<task_type>`) bound on routing key `task.<task_type>`,
  one DLQ per task type (`chronos.tasks.<task_type>.dlq`) bound to the
  DLX on the same routing key, primary queues carrying
  `x-dead-letter-exchange`/`x-dead-letter-routing-key` arguments.
  Declaration is idempotent (verified against both a fake channel and a
  real broker).
- **Config** (`mq/config.py`): `RabbitMQConfig`/`RelayConfig`, both
  `from_env()`-driven, no hard-coded credentials.
- **Publisher** (`mq/connection.py`): `RabbitMQPublisher`, real
  pika-backed, `confirm_delivery()` enabled, persistent delivery mode.
- **Outbox relay** (`relay/outbox_relay.py`): polls
  `SELECT_UNPUBLISHED_OUTBOX_EVENTS` (oldest-`created_at`-first, batched),
  publishes `{event_id, task_id, workflow_id, task_type}` as a dispatch
  hint, marks `published_at` only after a confirmed publish
  (`MARK_OUTBOX_EVENT_PUBLISHED`, conditional on `published_at IS NULL`),
  exponential backoff on failure, at-least-once delivery by design.

**Bug found and fixed during Phase 3 (before final sign-off):**
`RabbitMQPublisher.publish()` originally read `pika.BlockingChannel
.basic_publish()`'s return value and coerced it to `bool`. In
`confirm_delivery` mode, `basic_publish()` actually returns `None` on a
successful, broker-confirmed publish, and *raises*
(`NackError`/`UnroutableError`) on an unsuccessful one — it never returns
a bool. The original code's `bool(None)` therefore evaluated to `False`
for every genuinely successful publish, meaning `published_at` would
never have been set against a real broker. This was caught by a targeted
code audit (not by the unit tests, which used a fake publisher that
didn't reproduce pika's real contract), fixed by treating "`basic_publish`
completed without raising" as success (`return True`), catching
`NackError`/`UnroutableError` explicitly to return `False`, and mapping
any other `AMQPError` to `BrokerUnavailableError`. A regression test
(`tests/test_mq_connection.py::test_publish_returns_true_when_basic_publish_completes_without_raising`)
pins this exact contract using a fake that reproduces pika's real
return-`None`-on-success behavior. The real-broker integration test
(`tests/test_rabbitmq_integration.py::test_publish_with_confirm_against_real_broker`)
subsequently passed, validating the fix against an actual broker.

### 24.2 Phase 4 — RabbitMQ worker consumption: COMPLETE (to current scope)

Implemented exactly per the approved design (handoff section 17, with one
deliberate, explicitly-approved deviation and one deliberate deferral):

- **Targeted claim** (`db/persistence.py::claim_task_by_id`,
  `db/queries.py::CLAIM_TASK_BY_ID`): added as a prerequisite after a
  design reconciliation established that the original `claim_task()`
  (untargeted, "oldest READY task") is unsafe for dispatch-hint-driven
  consumption -- a worker holding `task_id=42` must never end up claiming
  an unrelated task 57. `claim_task_by_id` is a single conditional
  `UPDATE ... WHERE id = :task_id AND state = 'READY'`, verified safe
  under real concurrent-connection races (`tests/test_claiming_concurrency.py`).
  `claim_task()` itself is unchanged and preserved for a future Phase 5
  fallback poller.
- **Consumer** (`mq/connection.py::RabbitMQConsumer`): real pika-backed,
  manual ack (`auto_ack=False`), small configurable prefetch, one queue
  per worker process (one task type per worker, per the approved Phase 4
  design). Built on `BlockingChannel.consume()`'s generator form.
- **Message validation** (`worker/message.py`): validates the exact
  `{event_id, task_id, workflow_id, task_type}` shape; malformed messages
  are rejected (`requeue=False`) -- never trusted, never crash the
  consume loop -- and are routed to their task type's DLQ by RabbitMQ's
  own dead-letter routing (Phase 3's topology), not by any
  republish/dedup logic in the worker.
- **Execution abstraction** (`worker/executor.py`): the smallest useful
  `task_type -> callable` registry (`TaskExecutorRegistry`) -- no plugin
  framework, no sandboxing, no dynamic imports. An unregistered
  `task_type` is treated as an ordinary execution failure.
- **Glue** (`worker/task_worker.py::TaskWorker`): consume -> validate ->
  `claim_task_by_id` -> **ACK immediately, before execution** -> execute
  -> `report_task_success`/`report_task_failure`. Never falls back to
  `claim_task()`. Never mutates task state directly. Duplicate/stale
  dispatch hints are handled uniformly by `claim_task_by_id` returning
  `None` (no dedup table, no event-id tracking).
- **Heartbeats: deliberately deferred, NOT implemented.** No lease-renewal
  SQL was added; `core/transitions.py`'s lease fields are read but never
  extended by the worker. This was an explicit decision (confirmed with
  the user) since no reaper exists yet to consume lease-expiry behavior --
  building heartbeating now would be unvalidated against its only
  consumer. Revisit when Phase 5 (reaper) is built.
- **`expected_task_type` predicate on `claim_task_by_id`: deliberately
  NOT added.** Considered during the design reconciliation; no concrete
  correctness gap was found that required it, so the persistence API was
  kept minimal per explicit instruction.

**Bug found and fixed during Phase 4 (in test cleanup, not production
code):** `RabbitMQConsumer.close()` intentionally closes both its channel
and the underlying `BlockingConnection` (mirroring `RabbitMQPublisher
.close()`'s established, unit-tested contract -- see
`tests/test_mq_consumer.py::test_close_closes_channel_then_connection`).
Three tests in `tests/test_rabbitmq_worker_integration.py` additionally
closed the same raw connection object again in their `finally` blocks,
producing `pika.exceptions.ConnectionWrongStateError:
BlockingConnection.close(200, 'Normal shutdown') called on closed
connection` during cleanup -- *after* every behavioral assertion in each
test had already passed. This was a test-only resource-ownership bug, not
a defect in `RabbitMQConsumer`; the fix (removing the redundant
`consumer.close()`/`second_consumer.close()` calls, matching the existing
Phase 3 integration-test pattern of a single connection-level close) was
applied only to `tests/test_rabbitmq_worker_integration.py`. No
production code changed. All three tests (manual-ack consume, unacked-
message redelivery after a simulated crash, malformed-message
dead-lettering) subsequently passed against the real broker.

### 24.3 Final verified baseline

Established in a session where **both real infrastructure dependencies
were live simultaneously** -- no mocks, no testcontainers fallback, no
skips:

- **PostgreSQL**: real local instance, `chronos_test` database,
  `CHRONOS_TEST_DATABASE_URL` configured for the run.
- **RabbitMQ**: real local RabbitMQ 4.3.5 (Erlang/OTP 27),
  `amqp://guest:guest@localhost:5672/`, `CHRONOS_TEST_RABBITMQ_URL`
  configured for the run.

```
CHRONOS CHECKPOINT -- PHASE 1-4 INTEGRATED BASELINE

Phase 1:                COMPLETE (verified, real PostgreSQL)
Phase 2:                COMPLETE (verified, real PostgreSQL)
Phase 3:                COMPLETE (verified, real RabbitMQ 4.3.5)
Phase 4:                COMPLETE for claim/ack/execute/report
                         (verified, real RabbitMQ 4.3.5;
                         heartbeat deliberately deferred to Phase 5)

Tests collected:        217
Passed:                 217
Failed:                 0
Skipped:                0
Warnings:                0

PostgreSQL:              VERIFIED (real local instance, chronos_test)
RabbitMQ:                 VERIFIED (real local RabbitMQ 4.3.5 / Erlang OTP 27)
core/transitions.py:      UNCHANGED
  SHA-256:                3674492749fe3b4b04323f82be0b5e56f8a4a83cccef6aa7fbb5d7f8f207d20a

Reaper:                   NOT STARTED (Phase 5, next)
Fallback polling:         NOT STARTED (Phase 5, next)
Heartbeat:                NOT STARTED (deliberately deferred to Phase 5)
Redis:                    NOT STARTED (Phase 6)
API / Scheduler / Frontend: NOT STARTED
```

Note: a partial run with only `CHRONOS_TEST_DATABASE_URL` set (no
RabbitMQ) will show **212 passed, 5 skipped** -- the 5 skips are exactly
`tests/test_rabbitmq_integration.py` (2) and
`tests/test_rabbitmq_worker_integration.py` (3), both cleanly gated on
`CHRONOS_TEST_RABBITMQ_URL`. That is expected, correct behavior, not a
regression -- it is not the same as the 217/217 fully-integrated baseline
recorded above, which requires both environment variables set in the same
run.

**This 217/217 baseline is itself now superseded by section 25's
289/289 Phase 1-7 baseline below.**

---

## 25. Phase 5, 6 & 7 Completion — Verified Integrated Baseline

**READ THIS SECTION FIRST for the current state of the repository** (it
supersedes section 24's "read this first" for the same reason section 24
superseded sections 1-23 -- each checkpoint section obsoletes the
previous one; all are kept for historical narrative). This section
records what Phase 5 (reaper + fallback polling), Phase 6 (worker lease
heartbeat), and Phase 7 (Redis concurrency limiter) actually built, the
one real bug found along the way, and the final verified baseline with
all three real infrastructure dependencies (PostgreSQL, RabbitMQ, Redis)
live simultaneously.

### 25.1 Phase 5 — Reaper + fallback polling: COMPLETE

- **Reaper** (`reaper/reaper.py::Reaper`, `reaper/config.py::ReaperConfig`):
  discovers `RUNNING` tasks whose lease has expired
  (`db/queries.py::SELECT_EXPIRED_LEASES`, backed by the
  `idx_tasks_running_lease` partial index that already existed since
  Phase 1 for exactly this purpose) and reclaims each via the existing,
  unmodified `persistence.expire_lease` -- no new state-machine logic
  anywhere. `TransitionError` from a lost reaper-vs-worker (or
  reaper-vs-reaper) race is caught in `Reaper.run_once` and counted as an
  expected `lost_races` outcome, never allowed to crash the loop or abort
  the rest of a batch; a genuinely unexpected exception on one candidate
  is counted separately as `errors` and also does not abort the batch.
  `run_forever` backs off only on `errors`, never on `lost_races` (mirrors
  `relay/outbox_relay.py`'s existing backoff shape exactly).
- **Fallback poller** (`worker/fallback_poller.py::FallbackPoller`):
  obtains work via the existing, unmodified, *untargeted* `claim_task()`
  (never `claim_task_by_id`) -- for the one documented scenario "broker
  unavailable entirely" (handoff section 18). Reuses
  `worker.execution.execute_and_report` -- the exact same function
  `TaskWorker` calls -- so execution/reporting logic is never duplicated;
  the only difference between the two worker flavors is how a
  `TaskRecord` is obtained. Verified safe to run concurrently alongside a
  RabbitMQ-driven `TaskWorker` with zero double-claims (real
  multi-thread test, `tests/test_fallback_poller.py
  ::test_fallback_poller_and_targeted_claim_never_double_claim`).

No bugs found during Phase 5 that required a code fix beyond the planned
implementation itself.

### 25.2 Phase 6 — Worker lease heartbeat: COMPLETE

- **Persistence** (`db/persistence.py::renew_lease`,
  `db/queries.py::RENEW_LEASE`): a single atomic conditional `UPDATE`,
  fenced on `lease_owner = worker_id AND state = 'RUNNING' AND
  lease_expires_at >= now` (an INCLUSIVE boundary, deliberately matching
  `core.transitions._lease_is_valid`'s own inclusive rule used by
  `report_success`/`report_failure` -- NOT `expire_lease`'s exclusive
  rule, since heartbeat is a worker-side operation like the former, not a
  reaper-side reclaim like the latter). Touches only `lease_expires_at`
  (+ `updated_at`) -- never `state`, `attempt_count`, or `lease_owner`.
  Returns `None` (never raises) for every fencing failure; a genuine
  database-level failure propagates as an ordinary exception, distinct
  from a clean fencing `None`. No `core/transitions.py` change -- lease
  renewal isn't a state transition in that module's vocabulary at all.
- **Background renewal** (`worker/heartbeat.py::LeaseHeartbeat`): a
  daemon thread started on claim, calling `renew_lease` on an interval
  (default ~lease_duration/3), stopped via a `threading.Event` (prompt,
  not tied to the polling interval). A clean `None` result is treated as
  definitive, permanent fencing (stop renewing, log clearly); a raised
  exception is treated as transient (log, retry next interval, never
  concluded to mean the lease was lost). Deliberately introduces **no
  cancellation mechanism** -- losing the lease never aborts the
  in-progress synchronous executor call; the existing at-least-once/
  possible-duplicate-execution tradeoff (handoff sections 8/15) already
  accounts for this outcome.
- **Wiring** (`worker/execution.py::execute_and_report`): heartbeat is
  started immediately before `executors.execute(record)` and stopped
  (bounded -- `_HEARTBEAT_STOP_TIMEOUT_SECONDS = 5.0`, explicit, never
  the unbounded `join(timeout=None)` default) strictly BEFORE either
  `report_task_success`/`report_task_failure` call, in every code path
  (`lease_duration=None`, the default, disables heartbeating entirely,
  preserving pre-Phase-6 behavior exactly for any caller that doesn't
  opt in).

**Bug found and fixed during Phase 6 (before final sign-off, caught by a
dedicated post-implementation audit):** the first implementation stopped
the heartbeat in a `finally` block that ran *after* the report calls
(both report calls lived inside the same `try` the `finally` closed
over), the reverse of the intended ordering. This was proven not
data-corrupting (renewing a lease and clearing it are both fenced on
`lease_owner`/`state`, so whichever commits last simply reflects the
final truth) but was a confirmed deviation from the approved design, with
zero test coverage catching it. Fixed by restructuring
`execute_and_report` so heartbeat is stopped immediately after execution
finishes (success or exception) and strictly before either report call;
regression tests (`tests/test_worker_execution.py
::test_heartbeat_is_stopped_before_report_success`/
`_report_failure`) pin the exact call order using a fake heartbeat/spy,
and bounded-stop tests
(`test_stuck_heartbeat_does_not_block_reporting`/
`_does_not_block_failure_reporting`) prove a heartbeat that never
confirms it stopped can't hang `execute_and_report` and that reporting
still happens regardless.

### 25.3 Phase 7 — Redis concurrency limiter: COMPLETE

- **Config** (`redis_limiter/config.py::RedisLimiterConfig`): one default
  concurrency limit (`CHRONOS_REDIS_LIMITER_DEFAULT_LIMIT`) with optional
  per-task-type overrides (`CHRONOS_REDIS_LIMITER_LIMIT_<TASK_TYPE>`,
  matched case-insensitively by upper-casing both sides), a `slot_ttl_seconds`
  documented explicitly as an ADMISSION-CONTROL APPROXIMATION (not a
  correctness mechanism -- the PostgreSQL lease remains the correctness/
  ownership authority), and bounded connect/socket timeouts so an
  unreachable-but-not-immediately-refused Redis can't stall a call for
  the OS's default TCP timeout.
- **Limiter** (`redis_limiter/limiter.py::TaskTypeLimiter`): one Redis
  ZSET per task type, member `"<worker_id>:<task_id>"`, score = slot
  expiry timestamp. `acquire()` runs an atomic Lua script
  (`ACQUIRE_SLOT_SCRIPT`, inlined as a named Python string constant,
  matching `db/queries.py`'s SQL-as-constants convention) that prunes
  expired members (`ZREMRANGEBYSCORE ... -inf now`) then admits only if
  still under the limit -- atomicity is what prevents two concurrent
  acquirers from both observing "under limit" and both succeeding.
  `release()` is a plain `ZREM`. **Redis is never authoritative and every
  operation fails open**: any `RedisError` or other exception during
  `acquire()`/`release()` is caught, logged, and treated as "admission
  granted" / "nothing to clean up" -- Chronos must keep making progress
  even if Redis is completely down.
- **Wiring** (`worker/task_worker.py::TaskWorker`, optional `limiter`
  param): admission is checked BEFORE `claim_task_by_id` is ever called,
  using `task_type` from the (untrusted) dispatch hint only to select
  which Redis bucket to check -- never to decide any PostgreSQL-side
  outcome. Denial -> ACK the message, return, no claim attempt, no
  `attempt_count` increment, no state change. Granted-but-claim-returns-
  `None` -> release immediately (don't hold a slot for a claim that never
  happened). Granted-and-claimed -> hold the slot through
  `execute_and_report`, release exactly once afterward in a `finally`,
  regardless of success or failure.
- **Deliberately excluded from this phase** (both confirmed decisions,
  not implementation gaps): (1) `worker/fallback_poller.py` is NOT gated
  by the limiter -- it claims via the untargeted `claim_task()` and
  doesn't know a task's type until after claiming, so pre-claim admission
  gating is structurally impossible to do correctly there without
  guessing the type or claiming-then-releasing (which would burn a real
  attempt just to give the task back); (2) the Redis slot's TTL is NOT
  renewed by `worker/heartbeat.py::LeaseHeartbeat` -- it relies purely on
  a fixed TTL sized above typical execution time plus the acquire
  script's own stale-member pruning for crash recovery, matching the
  original handoff spec (section 19) exactly and keeping the two lease
  mechanisms (PostgreSQL correctness lease vs. Redis admission-control
  approximation) independent and unentangled.

**Bug found and fixed during Phase 7 (environment/compatibility, not
application logic):** the local Windows Redis instance available for
testing (`Redis.Redis` via winget -- the unofficial `tporadowski`/
Microsoft-archive Windows port, server version 3.0.504) does not support
the `HELLO` command, which `redis-py` (the installed client, 8.1.0)
issues by default to negotiate the RESP3 protocol on every new
connection. Every real-Redis call was failing with
`redis.exceptions.ResponseError: unknown command 'HELLO'` -- and because
`ResponseError` is a subclass of `RedisError`, `TaskTypeLimiter`'s
fail-open handling was silently catching this and returning "admission
granted" on every single call, meaning tests that merely asserted
"acquire succeeds" were passing for the wrong reason (fail-open, not a
real `ZADD`), while tests asserting real denial/pruning/capacity behavior
correctly failed. **Fix: pass `protocol=2` explicitly to
`redis.Redis.from_url(...)`** in both `redis_limiter/limiter.py`'s
production client construction and the equivalent raw client in
`tests/test_redis_limiter_integration.py`'s `redis_client` fixture. This
forces the classic RESP2 protocol and skips the `HELLO` handshake
entirely, so the client works against any RESP2-speaking server
(including this older/unofficial build) without depending on a specific
server version. **This changes only how the client talks to Redis -- no
change to limiter logic, Lua scripts, ZSET operations, or fail-open
behavior.** After the fix, `test_stale_slot_is_pruned_and_capacity_reclaimed`
(previously the one test still failing) was independently confirmed to be
exercising the real ZSET/Lua path rather than fail-open: it directly
inserts a stale member via the raw fixture client, asserts real
`zcard`/`zscore` state before and after, and asserts the stale member is
actually gone after a real `acquire()` call -- an outcome fail-open could
never produce, since fail-open never touches Redis at all.

### 25.4 Final verified baseline

Established in a session where **all three real infrastructure
dependencies were live simultaneously** -- no mocks, no testcontainers
fallback, no skips:

- **PostgreSQL**: real local instance, `chronos_test` database,
  `CHRONOS_TEST_DATABASE_URL` configured for the run.
- **RabbitMQ**: real local RabbitMQ 4.3.5 (Erlang/OTP 27),
  `amqp://guest:guest@localhost:5672/`, `CHRONOS_TEST_RABBITMQ_URL`
  configured for the run.
- **Redis**: real local Redis 3.0.504 (Windows port, installed via
  `winget install Redis.Redis`), `redis://localhost:6379/0`,
  `CHRONOS_TEST_REDIS_URL` configured for the run. No password configured
  (`requirepass` empty) on this local instance. Client connections use
  `protocol=2` (see 25.3) -- required for this specific server version,
  not a general Chronos requirement.

```
CHRONOS CHECKPOINT -- PHASE 1-7 INTEGRATED BASELINE

Phase 1:                COMPLETE (verified, real PostgreSQL)
Phase 2:                COMPLETE (verified, real PostgreSQL)
Phase 3:                COMPLETE (verified, real RabbitMQ 4.3.5)
Phase 4:                COMPLETE (verified, real RabbitMQ 4.3.5)
Phase 5:                COMPLETE (verified, real PostgreSQL) -- reaper + fallback poller
Phase 6:                COMPLETE (verified, real PostgreSQL) -- worker lease heartbeat
Phase 7:                COMPLETE (verified, real Redis 3.0.504) -- concurrency limiter

Tests collected:        289
Passed:                 289
Failed:                 0
Skipped:                0
Warnings:                0

PostgreSQL:              VERIFIED (real local instance, chronos_test)
RabbitMQ:                 VERIFIED (real local RabbitMQ 4.3.5 / Erlang OTP 27)
Redis:                    VERIFIED (real local Redis 3.0.504, protocol=2)
core/transitions.py:      UNCHANGED
  SHA-256:                3674492749fe3b4b04323f82be0b5e56f8a4a83cccef6aa7fbb5d7f8f207d20a

No files were modified during the final baseline verification run itself
-- all code changes (reaper, fallback poller, heartbeat, Redis limiter,
the heartbeat-ordering fix, and the protocol=2 fix) were made and
individually verified in prior steps; this run was a pure verification
pass against the already-final code.

API:                      NOT STARTED
Scheduler:                NOT STARTED
Frontend:                 NOT STARTED
Exactly-once delivery:    NOT IMPLEMENTED (deliberately -- not a goal)
Generic deduplication:    NOT IMPLEMENTED (deliberately -- not a goal)
Redis slot renewal from heartbeat: NOT IMPLEMENTED (deliberate scope
                           decision, see 25.3)
Redis admission control for fallback poller: NOT IMPLEMENTED (deliberate
                           scope decision, see 25.3)
Phase 8:                  NOT DEFINED / NOT STARTED
```

Note: a partial run with only `CHRONOS_TEST_DATABASE_URL` and
`CHRONOS_TEST_RABBITMQ_URL` set (no Redis) will show **282 passed, 7
skipped** -- the 7 skips are exactly `tests/test_redis_limiter_integration.py`,
cleanly gated on `CHRONOS_TEST_REDIS_URL`. That is expected, correct
behavior, not a regression -- it is not the same as the 289/289
fully-integrated baseline recorded above, which requires all three
environment variables set in the same run.
