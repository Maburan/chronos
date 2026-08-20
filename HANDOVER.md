# Chronos — Handover (for the next Claude session)

Written at the end of a session that completed Phase 12 and a publication
cleanup pass. This document exists so a **fresh Claude session tomorrow**
can pick up work without re-deriving context from scratch. Read this
before touching any code.

---

## 1. What Chronos is

Chronos is a distributed task-orchestration system: clients submit
workflows of dependent tasks over HTTP, and Chronos guarantees each task
is executed at least once, retried on failure up to a configured limit,
and recovered automatically if the worker executing it crashes — without
ever losing or duplicating the underlying state. **PostgreSQL is the
single source of truth throughout**; RabbitMQ and Redis are supporting
infrastructure that can fail, restart, or be removed without corrupting a
task's state.

See `README.md` (repo root) for the full technical explanation — this
handover summarizes it plus adds the "what to do next" and "what to watch
out for" context the README doesn't cover.

## 2. Current architecture and component roles

- **API** (`api/`) — FastAPI app. `POST /workflows`, `GET
  /workflows/{id}`, `POST /workflows/{id}/tasks`, `GET /tasks/{id}`, `GET
  /workflows/{id}/tasks`, `GET /workflows/{id}/status`, `GET /healthz`,
  `GET /readyz`. Never runs its own SQL — every route calls into
  `db/persistence.py`. No auth.
- **PostgreSQL** (`db/`) — the sole source of truth for task state, attempt
  counts, lease ownership/expiry, and the outbox. Schema in
  `db/migrations/*.sql`, hand-rolled migration runner in `db/migrate.py`.
  Concurrency safety comes entirely from Postgres's own locking (`FOR
  UPDATE SKIP LOCKED` for opportunistic claims, one conditional `UPDATE`
  for targeted claims) — nothing in RabbitMQ or Redis is relied on for
  correctness.
- **Outbox** (`outbox_events` table + `relay/outbox_relay.py`) — every
  READY-producing state transition writes an outbox row in the *same
  Postgres transaction* as the state change. A separate relay process
  polls for unpublished rows and republishes them to RabbitMQ, marking
  them published only after a broker confirm. This is what makes
  Postgres-commit and RabbitMQ-publish effectively atomic without a
  distributed transaction.
- **RabbitMQ** (`mq/`) — carries a dispatch *hint* only:
  `{event_id, task_id, workflow_id, task_type}`, never authoritative
  state. One durable topic exchange, one durable queue per task type, plus
  a dead-letter exchange/queue pair per type (declared, not yet consumed
  from — see §9). ACK happens immediately after a claim attempt resolves,
  *before* execution — RabbitMQ's job ends at "was this hint delivered,"
  never "did the task finish."
- **Workers** (`worker/task_worker.py`) — consume a dispatch hint, attempt
  a targeted atomic claim (`claim_task_by_id`), ACK, execute the
  registered task function, report success/failure back through the
  lease. `worker/executor.py` is a trivial `task_type -> callable`
  registry.
- **Leases/heartbeats** (`worker/heartbeat.py`, lease columns on `tasks`)
  — claiming sets `lease_owner`/`lease_expires_at`. A background daemon
  thread (`LeaseHeartbeat`) renews the lease periodically while executing.
  A worker's report of success/failure is only honored while it still
  holds a *valid, matching* lease — a stale/zombie report is silently
  ignored, never trusted. This is the core fencing mechanism.
- **Reaper** (`reaper/reaper.py`) — a separate, independent process/class
  that finds `RUNNING` tasks whose lease has expired and reclaims them
  through the identical retry-vs-terminal decision a worker-reported
  failure would go through. Never crashes on a lost race (e.g. the
  original worker reports success moments before the reaper acts) — that
  outcome is expected, counted, and handled, not an error.
- **Fallback poller** (`worker/fallback_poller.py`) — obtains work by
  polling Postgres directly (`claim_task()`, untargeted), never touching
  RabbitMQ. Exists for "the broker is down entirely." Shares the exact
  same execution/report code path as the RabbitMQ-driven worker
  (`worker/execution.py::execute_and_report`).
- **Redis limiter** (`redis_limiter/`) — optional, fail-open, per-task-type
  admission control (a Redis ZSET + Lua script). Any Redis failure means
  "admission granted" — Redis can never block dispatch.
- **Workflow status API** (`GET /workflows/{id}/status`) — a single
  PostgreSQL aggregation query (`db/queries.py::
  SELECT_WORKFLOW_TASK_STATE_COUNTS`) returning per-state counts,
  `is_complete` (true only once every task is terminal; an empty workflow
  is deliberately **not** complete), and `has_failures` (true iff at
  least one `FAILED_TERMINAL` task; `SKIPPED` never counts as a failure).

## 3. Completed phases/features

Phases 1–12 are all complete and verified. The most recent and most
relevant to future work:

- **Phase 9 (end-to-end runnability)** — real entrypoints exist:
  `python -m db` (schema bootstrap), `python -m relay`, `python -m
  worker`. One real registered demo task type, `echo` (see §7,
  `worker/demo_task.py`). A full HTTP-API-to-real-broker lifecycle was
  proven, including a manual smoke test with live background processes.
- **Phase 10 (operational readiness)** — graceful shutdown via the
  existing `stop_check` parameter + signal handlers (SIGINT/SIGTERM);
  correlation logging (`event_id` threaded from outbox row -> relay log
  line -> worker log line); optional `on_iteration` callbacks on
  `OutboxRelay.run_forever`/`TaskWorker.run_forever`; `GET /healthz`
  (liveness, no DB) and `GET /readyz` (503 if Postgres unreachable).
  **Known limitation carried forward**: graceful signal delivery was
  found unreliable on Windows during this phase and was *not* forced into
  an automated test — documented, not fixed, since this is a genuine
  platform limitation, not a bug in Chronos.
- **Phase 11 (crash recovery, proven for real)** — `tests/
  test_crash_recovery.py` spawns a real, separate `python -m worker` OS
  subprocess, lets it genuinely claim a task, **hard-kills the process**
  (`Popen.kill()` / `TerminateProcess` — not a graceful shutdown, not a
  simulated failure, not a direct row mutation), waits out the lease in
  real wall-clock time, runs the real `Reaper`, runs the real
  `OutboxRelay`, and has a second real worker consume the redelivery
  through to `SUCCEEDED`. This is the single strongest piece of evidence
  in the whole project — see §5.
- **Phase 12 (workflow status aggregation)** — `GET
  /workflows/{id}/status`, described in §2 above. Computed with one SQL
  query, no task rows fetched into Python for counting.

After Phase 12, a **publication cleanup pass** was done (no behavior
changed): added `README.md` (architecture, quickstart, crash-recovery
demo walkthrough, honest limitations section), removed "Phase N"
build-diary language from every production `.py` file's comments/
docstrings while preserving the underlying design rationale, and
relabeled `worker/demo_task.py` as an explicit demonstration task. Verify
before trusting any older internal notes that reference "Phase N" —
`CHRONOS_IMPLEMENTATION_HANDOFF.md` (the old build log, still present at
the repo root) still uses that language; `README.md` and the code
comments no longer do.

## 4. Current verified test baseline

**351 passed, 0 failed, 0 skipped, 0 warnings**, with real PostgreSQL,
real RabbitMQ, and real Redis all active simultaneously — no mocking of
any of the three in the integration/end-to-end suite. This was
last re-confirmed at the very end of the publication-cleanup pass (after
the comment/docstring edits, to prove they changed nothing behaviorally).

To reproduce, with all three real services reachable:

```bash
export CHRONOS_TEST_DATABASE_URL="postgresql://<user>:<password>@localhost:5432/chronos_test"
export CHRONOS_TEST_RABBITMQ_URL="amqp://guest:guest@localhost:5672/"
export CHRONOS_TEST_REDIS_URL="redis://localhost:6379/0"
python -m pytest -q
```

Without all three set, some real-infra test files skip cleanly (pure-logic
tests like `tests/test_transitions.py` never depend on any of them).

## 5. Strongest verified end-to-end behavior

The full chain has been proven against real infrastructure, not asserted
from code reading:

```
API -> PostgreSQL -> outbox -> RabbitMQ -> worker -> execution/reporting
```

...including:
- **Retry**: a task that fails is retried through the real outbox/relay/
  broker/worker chain and eventually succeeds, with `attempt_count`
  reflecting every attempt (`tests/test_end_to_end.py`).
- **Worker-crash recovery**: lease expiration -> reaper reclaim -> new
  outbox event -> relay republish -> redelivery -> a second real worker
  completing the task, driven by a genuinely killed OS process, not a
  simulation (`tests/test_crash_recovery.py` — see §3, Phase 11).

If asked to demo Chronos live, this crash-recovery path is the thing to
show — see `README.md`'s "Crash Recovery Demo" section for the exact
step-by-step commands.

## 6. Design decisions and invariants future work must not break

These are load-bearing. Breaking any of them silently would reintroduce
exactly the bug class Chronos was built to avoid:

1. **PostgreSQL is the sole source of truth.** Never let RabbitMQ or
   Redis state override a Postgres row, anywhere, for any reason.
2. **The outbox write and its state-transition write are atomic** — same
   transaction, always. Never split them across two commits.
3. **Fencing by exact lease ownership** (`lease_owner == worker_id AND
   now <= lease_expires_at`) is the only thing that authorizes a report or
   renewal to mutate a RUNNING task.
4. **`attempt_count` increments exactly once, at claim, in exactly one
   code path** (`core.transitions.claim`). Every retry-vs-terminal
   decision depends on this never happening twice or being skipped.
5. **Every claim is atomic and conditional** (`FOR UPDATE SKIP LOCKED` or
   a single conditional `UPDATE`). This is the entire defense against two
   workers executing the same task — nothing else in the system provides
   it.
6. **`core/transitions.py` is frozen.** It has not been modified across
   the entire project (12 phases) and its SHA-256 has been checked after
   every single one. It is pure, dependency-free, and the sole owner of
   state-machine legality. If a future task seems to require changing it,
   stop and reconsider the approach first — that has never been necessary
   so far.
7. **RabbitMQ ACK is never authoritative.** A message is ACKed
   immediately after a claim attempt resolves, before execution — this is
   deliberate, not a bug to "fix" by moving the ACK later.

## 7. Important files/modules

- `core/transitions.py` — the frozen, pure state machine. Read this first
  to understand the actual state model (`PENDING`, `READY`, `RUNNING`,
  `SUCCEEDED`, `FAILED_TERMINAL`, `SKIPPED`).
- `db/persistence.py` — every stateful operation Chronos performs, as one
  function per operation (`create_task`, `claim_task`,
  `claim_task_by_id`, `renew_lease`, `report_task_success`,
  `report_task_failure`, `expire_lease`, `get_workflow_task_state_counts`,
  `check_connection`, ...). `db/queries.py` holds every SQL string as a
  named constant.
- `db/migrations/0001_init.sql`, `0002_outbox.sql` — the whole schema.
  CHECK constraints here mirror `TaskInstance.__post_init__`'s invariants
  exactly, as a second line of defense.
- `mq/topology.py` — RabbitMQ exchange/queue/DLX naming and declaration
  (idempotent). `mq/connection.py` — the only module that imports `pika`
  outside of tests.
- `relay/outbox_relay.py` — the outbox-to-RabbitMQ bridge.
- `worker/task_worker.py` — the RabbitMQ-driven worker.
  `worker/execution.py::execute_and_report` — the execution/report glue
  shared by the RabbitMQ worker and the fallback poller.
  `worker/heartbeat.py` — the background lease-renewal thread.
  `worker/demo_task.py` — **demonstration task type only** (`echo`), not
  production code; see its docstring.
- `reaper/reaper.py` — the lease reaper.
- `redis_limiter/limiter.py` — the optional fail-open concurrency
  limiter.
- `api/app.py`, `api/routes.py`, `api/schemas.py`, `api/health.py` — the
  HTTP layer, a thin front door onto `db/persistence.py` only.
- `db/__main__.py`, `relay/__main__.py`, `worker/__main__.py` — the three
  real process entrypoints (`python -m db`/`relay`/`worker`). **There is
  no `reaper/__main__.py` or `api/__main__.py`** — see §9.
- `tests/conftest.py` — real-Postgres fixtures (`db`, `db_dsn`,
  `api_client`), resolved from `CHRONOS_TEST_DATABASE_URL` (or a
  testcontainers fallback, or a clean skip).
- `tests/test_crash_recovery.py` — the Phase 11 proof; read this before
  changing anything about worker/lease/reaper timing.
- `README.md` — the up-to-date, publication-facing technical
  documentation. `CHRONOS_IMPLEMENTATION_HANDOFF.md` — the old internal
  phase-by-phase build log; historically accurate but not maintained in
  the same voice as the README going forward.

## 8. Local development / run instructions

Chronos assumes PostgreSQL, RabbitMQ, and (optionally) Redis are already
running and reachable locally. It makes no assumption about how you run
them (native install, containers, etc.).

```bash
# Create an (empty) database for Chronos to use.
createdb chronos

# Required environment variables:
export CHRONOS_DATABASE_URL="postgresql://postgres:<password>@localhost:5432/chronos"
export CHRONOS_RABBITMQ_URL="amqp://guest:guest@localhost:5672/"
export CHRONOS_TASK_TYPES="echo"          # declared by the relay at startup
export CHRONOS_WORKER_ID="worker-1"
export CHRONOS_WORKER_TASK_TYPE="echo"

# 1. Bootstrap the schema (idempotent).
python -m db

# 2. Start the outbox relay (own terminal; runs forever).
python -m relay

# 3. Start a worker (own terminal; runs forever).
python -m worker

# 4. Start the API (own terminal; runs forever).
uvicorn "api.app:create_app" --factory --host 127.0.0.1 --port 8000
```

Full details, including a minimal `curl` walkthrough and the complete
crash-recovery demo script, are in `README.md`.

For running tests against real infrastructure, see §4 above.

## 9. What is NOT implemented yet

Be honest about these — do not assume they exist without checking:

- **No API authentication of any kind.**
- **No task cancellation.** Once claimed, a task runs to completion or
  until its lease expires; nothing can tell it to stop early.
- **No metrics/observability beyond structured logs.** No `/metrics`
  endpoint, no dashboards, no alerting.
- **No dead-letter inspection or replay tooling.** DLQs are declared and
  correctly routed to, but nothing consumes, inspects, or replays them.
- **No standalone reaper process.** `reaper/reaper.py`'s `Reaper` class is
  fully implemented and tested but has no `python -m reaper` entrypoint —
  it's run directly (see the README's crash-recovery demo for the exact
  `python -c` invocation pattern).
- **The Redis limiter is not wired into `python -m worker`.** It's fully
  implemented and tested (`tests/test_worker_task_worker_redis.py`,
  `tests/test_redis_limiter_integration.py`) but using it today means
  constructing `TaskTypeLimiter` and passing `limiter=` to `TaskWorker`
  yourself.
- **The fallback poller has no standalone entrypoint either** — same
  situation as the reaper.
- **Untested failure shapes**: clock skew between machines, network
  partitions (as opposed to a clean process kill), and RabbitMQ/Postgres
  restarting mid-operation are not exercised by any test. The
  crash-recovery test proves a clean process kill is recovered correctly
  — that is a narrower claim than "handles all worker failure modes."
- **No frontend of any kind exists yet.** This is the next planned task
  — see §10.

## 10. Next planned task: a React + TypeScript + Vite frontend

The next piece of work (not started, not yet planned in detail) is a
polished frontend/dashboard for Chronos that **visually demonstrates**:
workflow creation, task creation, state transitions, retries, and
crash recovery.

### 11. Intended frontend scope

- **Dashboard / system health** — likely built on `GET /healthz` and
  `GET /readyz`, plus maybe a simple list of recent workflows.
- **Workflow creation** — a form calling `POST /workflows`.
- **Workflow status view** — calling `GET /workflows/{id}/status`,
  visualizing the per-state counts, `is_complete`, and `has_failures`.
- **Task creation** — a form calling `POST /workflows/{id}/tasks`,
  including `task_type`, `max_attempts`, `payload`, and `depends_on_ids`.
- **Live polling** — the frontend will need to poll `GET /tasks/{id}` /
  `GET /workflows/{id}/tasks` / `GET /workflows/{id}/status` on an
  interval, since there is no push/websocket mechanism in the API today
  (and adding one would be backend work, out of scope unless explicitly
  requested later).
- **Task state visualization** — some visual representation of each
  task's current state (`PENDING`/`READY`/`RUNNING`/`SUCCEEDED`/
  `FAILED_TERMINAL`/`SKIPPED`) and how it changes over time.
- **Retry demonstration** — using `echo_task`'s `fail_until_attempt`
  payload knob (see `worker/demo_task.py`) to visually show a task
  retrying and eventually succeeding.
- **Crash-recovery visualization** — the centerpiece. Likely needs to
  show, over time: task RUNNING -> (worker killed, out of band — the
  frontend cannot kill a process itself) -> task still RUNNING -> lease
  expires -> reaper reclaims -> READY -> redispatched -> RUNNING again ->
  SUCCEEDED. Since killing a worker process is inherently an out-of-band,
  operator action (see README's crash demo), the frontend's job is likely
  to make the *state transitions* visible and legible in real time via
  polling, not to trigger the crash itself — this needs a concrete design
  decision before implementation starts.

### 12. Explicit constraint

**The frontend should reuse the existing API and should NOT modify
Chronos orchestration/business logic unless absolutely necessary.** If
the frontend's needs genuinely require a new read-only endpoint (e.g.
something like "list recent workflows," which does not exist yet — see
§13), that should be raised explicitly and treated as a small, deliberate
backend addition, not folded silently into frontend work. Nothing in
`core/transitions.py`, `db/persistence.py`'s existing functions, the
outbox, lease/heartbeat, reaper, or worker-claiming logic should change
for frontend purposes.

### 13. Known caveats to inspect before starting frontend work

- **There is currently no "list workflows" endpoint** — only `GET
  /workflows/{id}` (single, by id). A dashboard showing multiple
  workflows will need either a new endpoint (see the constraint above —
  raise this explicitly rather than assuming it's fine to add) or a
  different UX (e.g. the user always navigates by a known workflow id).
- **No CORS configuration exists on the API today.** A separately-served
  frontend (Vite dev server on a different port) will hit CORS errors
  against the FastAPI app until `CORSMiddleware` (or equivalent) is added
  — confirm this is acceptable "frontend work" or whether it counts as
  the kind of backend change that needs explicit sign-off first.
- **No push/websocket mechanism exists** — live updates must be polling-
  based. Pick a poll interval that's responsive enough to feel "live" for
  a demo without hammering the API.
- **Crash recovery cannot be triggered by the frontend itself** — killing
  a worker process is an OS-level action outside any HTTP endpoint. The
  demo will likely need an operator (or a documented manual step) to kill
  a worker while the frontend visualizes the resulting state changes.
- **`worker/demo_task.py`'s knobs are the only way to deterministically
  demo retries/long-running tasks today** (`fail_until_attempt`,
  `sleep_seconds`, `sleep_until_attempt`). The frontend's "create task"
  form will likely want to expose these for demo purposes, clearly
  labeled as demo-only, not as if they were real task-type semantics.
- **Windows environment**: this project has been developed and run on
  Windows (win32). PowerShell tool syntax differs from POSIX; be careful
  mixing Bash-tool and PowerShell-tool commands in the same session (both
  are available but have different syntax rules — see the environment's
  own tool descriptions).
- Before writing any frontend code, re-verify the exact current API
  response shapes by reading `api/schemas.py` directly rather than
  trusting this document's summary — schemas are a likely place for
  small drift between what's written here and what the code actually
  returns by the time frontend work begins.
