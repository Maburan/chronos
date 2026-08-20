# Chronos

Chronos is a distributed task-orchestration system: clients submit workflows
of dependent tasks over HTTP, and Chronos guarantees each task is executed
at least once, retried on failure up to a configured limit, and recovered
automatically if the worker executing it crashes — without ever losing or
duplicating the underlying state. PostgreSQL is the single source of truth
throughout; RabbitMQ and Redis are supporting infrastructure that can fail,
restart, or be removed without corrupting a task's state.

## The problem it solves

Any system that dispatches work to background workers has to answer three
questions honestly, not just optimistically:

- **What happens if the worker crashes mid-task?** Most naive designs leave
  the task stuck forever, or double-execute it on every restart.
- **What happens if the message broker and the database disagree?** A queue
  message and a database row are two different systems; writing to both is
  not atomic unless something makes it atomic.
- **What happens if two workers grab the same task at once?** Without an
  explicit, atomic claim, "exactly one worker" is a hope, not a guarantee.

Chronos answers all three with a small set of enforced invariants (below),
proven against real PostgreSQL, real RabbitMQ, and real Redis — including a
test that kills a real worker process mid-execution and shows the system
recovering the task on its own.

## Architecture

```mermaid
flowchart LR
    Client([Client / Frontend]) -->|HTTP| API[FastAPI]
    API -->|create workflow / task| PG[(PostgreSQL<br/>source of truth)]
    PG -->|same transaction| OUTBOX[[outbox_events]]
    OUTBOX -->|poll unpublished| RELAY[Outbox Relay]
    RELAY -->|publish + broker confirm| MQ[(RabbitMQ<br/>dispatch hints only)]
    MQ -->|dispatch hint: task_id| WORKER[Worker]
    WORKER -->|claim / fence / report| PG
    REAPER[Reaper] -->|reclaim expired leases| PG
    REDIS[(Redis<br/>optional, fail-open)] -.per-task-type<br/>admission check.-> WORKER
```

Every arrow into PostgreSQL is authoritative. Every other arrow is a hint
or an optimization — RabbitMQ tells a worker "task X *might* be claimable,
go try"; it never tells anyone what state a task is actually in.

## The complete execution flow

```
API
  -> PostgreSQL            task row created (READY if no deps, else PENDING)
  -> transactional outbox  one TASK_READY event, same transaction as above
  -> Outbox Relay          publishes the event, marks it published only
                           after a broker confirm
  -> RabbitMQ              durable, per-task-type queue; at-least-once
  -> Worker                consumes the dispatch hint (task_id only)
  -> PostgreSQL claim      atomic, conditional UPDATE: READY -> RUNNING,
     / fencing             attempt_count+1, lease_owner + lease_expires_at set
  -> execution / report    the registered task function runs; success or
                           failure is reported back through the lease
  -> retry OR terminal     failure: attempts remain -> READY (new outbox
     failure                event); attempts exhausted -> FAILED_TERMINAL
                           (cascades SKIPPED to dependents)
  -> Reaper                if a worker dies mid-task, the lease eventually
     (abandoned leases)    expires; the reaper reclaims it exactly like a
                           worker-reported failure would
```

### PostgreSQL: source of truth

Every fact Chronos relies on lives in PostgreSQL: task state, attempt
counts, lease ownership, lease expiry, and the outbox. Nothing downstream
(RabbitMQ, Redis) is ever trusted over a Postgres row. Concurrency safety
comes entirely from Postgres's own locking (`FOR UPDATE SKIP LOCKED` for
opportunistic claims, a single conditional `UPDATE` for targeted ones) —
not from anything RabbitMQ or Redis does.

### RabbitMQ and Redis: supporting infrastructure, not authoritative state

RabbitMQ carries a dispatch *hint* — `{event_id, task_id, workflow_id,
task_type}` — never task state. A worker receiving a stale, duplicate, or
already-resolved hint just finds nothing claimable and moves on; nothing
breaks. Redis (optional) only ever answers "is there capacity to start one
more `<task_type>` right now?" — it never decides which task runs, and any
Redis failure fails open (admission granted) rather than blocking dispatch.
If RabbitMQ or Redis were deleted entirely, task correctness would be
unaffected; only dispatch latency and concurrency limiting would change.

### The transactional outbox — and why it exists

Writing "the task is READY" to Postgres and "tell RabbitMQ about it" to a
broker are two different systems. Doing them as two separate operations
creates the classic **dual-write problem**: a crash between the two either
loses the dispatch (task READY forever, nobody told) or, done in the wrong
order, publishes a message for a transition that then rolls back.

Chronos avoids this by writing an `outbox_events` row in the **same
Postgres transaction** as the state change that produced it — both commit
together or neither does. A separate Outbox Relay process independently
polls for unpublished events and republishes them, marking a row published
only after the broker confirms it. This makes delivery **at-least-once**,
never at-most-once and never silently zero-once — a duplicate dispatch is
harmless (see claiming, below); a lost one is not possible without also
losing the Postgres commit that would have produced it.

### Lease-based ownership and fencing

Claiming a task sets `lease_owner` and `lease_expires_at` on the row. A
worker renews its lease periodically via a background heartbeat while
executing. Critically, a worker's *report* of success or failure is only
honored if it still holds a **valid** lease at that moment — the exact
worker_id, not expired. A report from a worker that lost its lease (because
it was reclaimed by the reaper, or another attempt already resolved the
task) is silently ignored, never trusted. This is what makes crash recovery
safe: a zombie worker that wakes up late and tries to report can never
corrupt a task that has already moved on.

### Crash recovery

If a worker dies mid-task, nothing runs to clean up after it — no
handler, no cleanup hook. The task simply stays `RUNNING` under a lease
that stops being renewed. Once that lease expires, the **reaper** — a
separate, independent process — finds it via a plain read (`RUNNING` tasks
with an expired `lease_expires_at`) and reclaims it through the exact same
retry-vs-terminal decision a worker-reported failure would go through:
attempts remaining -> back to `READY` (a fresh outbox event, so it gets
redispatched); attempts exhausted -> `FAILED_TERMINAL`. The reaper never
crashes on a race it loses (e.g. the original worker reports success a
moment before the reaper acts) — that outcome is expected and handled, not
treated as an error.

### Retries and dependency skip propagation

`attempt_count` is incremented exactly once, at claim time, and never
again. On failure — reported by a worker or detected by the reaper — the
same rule decides what happens next: attempts remaining means the task goes
back to `READY`; attempts exhausted means `FAILED_TERMINAL`. A task that
reaches `FAILED_TERMINAL` cascades `SKIPPED` to every task that (directly
or transitively) depends on it, so a workflow's dependency graph never gets
stuck waiting on a dependency that can no longer succeed.

### Redis task-type concurrency limiting (fail-open)

Chronos includes an optional per-task-type admission control layer backed
by Redis (`redis_limiter/`): before claiming a task, a worker can check
whether the configured concurrency limit for that task type has already
been reached, and skip the claim attempt if so. It is deliberately
**fail-open** — any Redis error, timeout, or unavailability is treated as
"admission granted," so Redis can never block task dispatch by being down.
It is fully implemented and tested against a real Redis instance, but is
**not wired into the `python -m worker` entrypoint** shipped in this repo
— using it today means constructing a `TaskTypeLimiter` and passing it to
`TaskWorker(..., limiter=...)` directly, as the tests do
(`tests/test_worker_task_worker_redis.py`).

### The fallback polling path

`worker/fallback_poller.py` provides a second way to obtain work that
never touches RabbitMQ at all: it polls PostgreSQL directly for the oldest
claimable task (`claim_task()`, the untargeted counterpart to the
RabbitMQ-driven worker's targeted `claim_task_by_id()`). It exists for one
scenario — the broker is unavailable entirely — and shares the exact same
execution/reporting code path as the RabbitMQ-driven worker, so its
behavior once a task is claimed is identical. It is safe to run alongside
any number of RabbitMQ-driven workers concurrently; Postgres's conditional
claim is what makes that safe. Like the Redis limiter, it has no standalone
`python -m` entrypoint in this repo today — it's a tested, ready-to-use
class, not a shipped process.

### `GET /workflows/{id}/status`

A caller-facing rollup: total task count plus a count per state (`pending`,
`ready`, `running`, `succeeded`, `failed_terminal`, `skipped`), an
`is_complete` flag (true only once every task has reached a terminal
state — `SUCCEEDED`, `FAILED_TERMINAL`, or `SKIPPED`; an empty workflow is
deliberately **not** considered complete), and a `has_failures` flag (true
if at least one task is `FAILED_TERMINAL`; `SKIPPED` tasks never count as a
failure on their own). The whole thing is computed by a single PostgreSQL
aggregation query — no task rows are fetched into Python to be counted.

## Quick Start

Chronos assumes PostgreSQL, RabbitMQ, and (optionally) Redis are already
running and reachable — however you run them locally is up to you; Chronos
itself makes no assumption beyond a working connection string for each.

```bash
# Postgres: create an (empty) database for Chronos to use.
createdb chronos

# Environment variables Chronos reads (see mq/config.py, api/config.py):
export CHRONOS_DATABASE_URL="postgresql://postgres:<password>@localhost:5432/chronos"
export CHRONOS_RABBITMQ_URL="amqp://guest:guest@localhost:5672/"
export CHRONOS_TASK_TYPES="echo"          # declared by the relay at startup
export CHRONOS_WORKER_ID="worker-1"
export CHRONOS_WORKER_TASK_TYPE="echo"

# 1. Bootstrap the schema (idempotent -- safe to run again later).
python -m db

# 2. Start the outbox relay (own terminal; runs forever).
python -m relay

# 3. Start a worker (own terminal; runs forever).
python -m worker

# 4. Start the API (own terminal; runs forever).
uvicorn "api.app:create_app" --factory --host 127.0.0.1 --port 8000

# 5. Start the dashboard frontend (own terminal; separate dev server).
cd frontend
npm install    # first time only
npm run dev    # -> http://localhost:5173
```

With all four running, create a workflow and a task, then poll its status:

```bash
# Create a workflow.
curl -s -X POST http://127.0.0.1:8000/workflows \
  -H "Content-Type: application/json" \
  -d '{"name": "demo"}'
# -> {"id": 1, "name": "demo", "created_at": "..."}

# Create a task in it (task_type "echo" is the one registered demo task
# type -- see worker/demo_task.py).
curl -s -X POST http://127.0.0.1:8000/workflows/1/tasks \
  -H "Content-Type: application/json" \
  -d '{"name": "t1", "task_type": "echo", "payload": {"message": "hello"}}'
# -> {"id": 1, "state": "READY", ...}

# Poll the workflow's rollup status until it's complete.
curl -s http://127.0.0.1:8000/workflows/1/status
# -> {"workflow_id": 1, "total_tasks": 1, "succeeded": 1, ...,
#     "is_complete": true, "has_failures": false}
```

## Frontend / Dashboard

`frontend/` is a React + TypeScript + Vite dashboard that exercises the
same HTTP API above — it has no privileged access, no direct database or
broker connection, and adds no server-side behavior. It exists to make
the execution flow (`PENDING/READY -> RUNNING -> SUCCEEDED /
FAILED_TERMINAL / SKIPPED`) visible without reading logs or writing curl
commands by hand.

What it shows, all sourced from the existing endpoints (`POST
/workflows`, `GET /workflows/{id}`, `POST /workflows/{id}/tasks`, `GET
/tasks/{id}`, `GET /workflows/{id}/tasks`, `GET
/workflows/{id}/status`, `GET /healthz`, `GET /readyz`):

- **Workflow creation and lookup** — there is no "list workflows"
  endpoint, so the dashboard navigates by a known workflow id (created
  or loaded), same as any other API client would have to.
- **A static pipeline diagram** (API -> Postgres/Outbox -> Outbox Relay
  -> RabbitMQ -> Worker -> Task Execution) explaining the architecture
  above at a glance — it does not claim to monitor RabbitMQ or the
  worker process individually, since no endpoint exposes that.
- **Live task state** via 2-second polling of `GET
  /workflows/{id}/tasks` and `.../status` — there is no push/WebSocket
  channel in the API, so polling is the only mechanism available, and
  the dashboard is explicit about that rather than implying otherwise.
- **A task detail panel** with attempt counts, lease owner/expiry, full
  payload, and a lifecycle stepper derived from the task's *current*
  state — not a fabricated event history, since the API only returns
  the current row.
- **A dependency graph** built from `depends_on_ids` submitted through
  the dashboard itself in the current browser session (the API has no
  endpoint to read a task's dependency edges back out) — tasks whose
  edges weren't submitted this session are shown as explicitly
  unverified, dashed nodes, never silently presented as edge-free.
- **A one-click retry-demo helper** that fills the task payload with
  `{"message": "retry demo", "fail_until_attempt": 3}` (see
  `worker/demo_task.py`'s `echo` task type) without submitting it, so a
  retry cycle (`attempt_count` climbing 1 -> 2 -> 3 before succeeding)
  can be triggered and watched on demand.

The frontend requires CORS enabled on the API for its own origin
(`http://localhost:5173` by default — see `api/config.py`'s
`cors_origins` / `CHRONOS_API_CORS_ORIGINS`) and otherwise makes no
changes to backend behavior.

## Crash Recovery Demo

This is the centerpiece of the project: proof, not a diagram, that a
crashed worker's task is recovered automatically. It mirrors exactly what
`tests/test_crash_recovery.py` proves automatically — this section just
walks through it by hand, live.

**1. Start the relay and a worker** (as in Quick Start), with a short lease
so the demo doesn't take long:

```bash
export CHRONOS_WORKER_LEASE_SECONDS="10"
python -m worker
```

**2. Submit a task that runs long enough to observe.** `echo_task`'s
`sleep_seconds` payload knob (see `worker/demo_task.py`) makes it block
before finishing — long enough to kill the worker while the task is still
genuinely executing:

```bash
curl -s -X POST http://127.0.0.1:8000/workflows/1/tasks \
  -H "Content-Type: application/json" \
  -d '{"name": "crash-demo", "task_type": "echo", "max_attempts": 2,
       "payload": {"message": "will survive a crash", "sleep_seconds": 20}}'
```

**3. Watch it become `RUNNING`:**

```bash
curl -s http://127.0.0.1:8000/tasks/2
# -> {"state": "RUNNING", "lease_owner": "worker-1", "attempt_count": 1, ...}
```

**4. Kill the worker process — a real crash, not a graceful shutdown.**
Find its PID (printed at startup, or via your OS's process list) and force
it:

```bash
kill -9 <pid>                       # Linux/macOS
taskkill /F /PID <pid>              # Windows
```

**5. Observe the task remaining `RUNNING`** for as long as the lease is
still valid — nothing recovers it yet, by design:

```bash
curl -s http://127.0.0.1:8000/tasks/2
# -> still {"state": "RUNNING", "lease_owner": "worker-1", ...}
```

**6. Wait past the lease duration** (10s above), **then run the reaper.**
There is no standalone reaper process shipped in this repo yet, so this
uses the existing `Reaper` class directly, exactly as
`tests/test_crash_recovery.py` and `tests/test_reaper.py` do:

```bash
python -c "
from db.connection import get_pool
from reaper.config import ReaperConfig
from reaper.reaper import Reaper
import os

pool = get_pool(os.environ['CHRONOS_DATABASE_URL'])
result = Reaper(pool, config=ReaperConfig.from_env()).run_once()
print(result)
"
# -> ReaperIterationResult(candidates=1, reclaimed=1, lost_races=0, errors=0)
```

**7. Observe the task reclaimed to `READY`**, and a new outbox event
produced for it:

```bash
curl -s http://127.0.0.1:8000/tasks/2
# -> {"state": "READY", "lease_owner": null, "attempt_count": 1, ...}
```

**8. The outbox event is republished** the next time the relay (still
running from step 1) polls — no action needed, it happens automatically
within `CHRONOS_RELAY_POLL_INTERVAL_SECONDS` (default 1s).

**9. Start another worker to consume the redelivery:**

```bash
python -m worker
```

**10. Observe the final state:**

```bash
curl -s http://127.0.0.1:8000/tasks/2
# -> {"state": "SUCCEEDED", "attempt_count": 2, "lease_owner": null, ...}
```

The task survived a hard process kill mid-execution with no manual
intervention beyond running the reaper (which, in a real deployment, would
itself be a long-running process, not a one-off script).

## Why the design works

- **Transactional outbox / dual-write problem.** Writing to two different
  systems (Postgres, RabbitMQ) can never be made atomic by writing to both
  directly. Chronos sidesteps the problem instead of solving it head-on:
  only Postgres is written synchronously (with the outbox row, same
  transaction); RabbitMQ is written to asynchronously, by a separate
  process, from data that is already durably committed. There is no
  window in which a committed task state and its dispatch event can
  disagree.
- **PostgreSQL as sole source of truth.** Every other component's state
  (a RabbitMQ message existing, a Redis slot being held) is a hint that
  can be wrong, stale, or duplicated without consequence, because nothing
  downstream ever treats it as authoritative. This is what makes the rest
  of the list possible.
- **Atomic conditional claiming.** `FOR UPDATE SKIP LOCKED` (untargeted)
  and a single conditional `UPDATE ... WHERE state = 'READY'` (targeted)
  are the *entire* defense against two workers executing the same task.
  Two workers racing for the same row never both succeed; Postgres's own
  MVCC/locking guarantees that, not application-level coordination.
- **Lease fencing.** A worker's report of success or failure is only
  honored while it still holds a live, matching lease. This is what turns
  "a worker crashed" from a correctness problem into an operational one:
  a zombie worker reporting late is simply ignored, never trusted over
  whatever has happened to the task since.
- **RabbitMQ ACK is not authoritative for correctness.** A message is
  ACKed the moment a claim attempt resolves — before execution even
  starts — because the ACK's only job is "was this hint delivered," never
  "did the task finish." Correctness never depends on RabbitMQ's
  redelivery behavior, ACK timing, or even RabbitMQ being reachable at
  all (see the fallback poller).
- **Retry semantics live in one place.** Both a worker-reported failure
  and a reaper-detected lease expiry resolve through the identical
  retry-vs-terminal decision (`attempts remaining -> READY`, `exhausted
  -> FAILED_TERMINAL`), so there is exactly one definition of "should
  this task retry," never two that could drift apart.
- **Crash recovery is proven, not assumed.** `tests/test_crash_recovery.py`
  kills a real, separate OS process mid-execution and shows the reaper,
  outbox, relay, and a second worker recovering the task through real
  PostgreSQL and real RabbitMQ — not a mock, not a simulated failure.

## Testing

**351 tests, 0 failures, 0 skipped, 0 warnings**, run against real
PostgreSQL, real RabbitMQ, and real Redis simultaneously (no service is
mocked in the integration/end-to-end suite; unit tests for pure logic —
e.g. `core/transitions.py` — use no infrastructure at all, by design).

Among those 351: real concurrency tests that race a live reaper against a
live worker reporting completion using genuinely separate database
connections and threads, and — the most significant single test in the
suite — `tests/test_crash_recovery.py`, which spawns a real `python -m
worker` OS subprocess, lets it claim a task for real, hard-kills the
process mid-execution, waits out the lease in real wall-clock time, and
verifies the reaper/outbox/relay/second-worker chain recovers it to the
correct terminal state.

What this suite does **not** claim to prove: behavior under network
partitions (as opposed to a clean process kill), clock skew between
machines, or a broker/database restarting mid-operation. See "Known
limitations" below.

## Known limitations

- **No API authentication.** Every endpoint is open; this is not
  acceptable for any real deployment.
- **No task cancellation.** Once claimed, a task cannot be told to stop;
  it runs to completion or until its lease expires.
- **Limited observability.** Structured logs exist throughout, but there
  is no metrics endpoint, dashboard, or alerting — diagnosing a production
  issue today means reading logs or querying Postgres by hand.
- **No dead-letter inspection or replay tooling.** Poison messages are
  correctly routed to a per-task-type dead-letter queue, but nothing
  consumes, inspects, or replays them.
- **Clock-skew assumptions are untested.** Lease comparisons assume the
  reaper and every worker agree closely enough on wall-clock time; this
  has not been exercised under real clock drift between machines.
- **Network-partition and broker/database-restart scenarios are not
  fully exercised.** The crash-recovery test proves a clean process kill
  is recovered correctly; a worker that goes unreachable but keeps
  running (a partition, not a crash), or RabbitMQ/Postgres restarting
  mid-operation, are different failure shapes that have not been tested
  the same way.
- **The Redis limiter and the fallback poller have no standalone
  entrypoint.** Both are fully implemented and tested, but using either
  today means writing a small amount of wiring code yourself (see the
  sections above) rather than running a shipped command.

---

## Publication checklist

- [x] README — this file
- [x] Architecture diagram — Mermaid diagram + explicit execution-flow list
- [x] Quick Start — accurate env vars/commands/ports, taken directly from the current code
- [x] Crash Recovery Demo — step-by-step, using only existing repository mechanisms
- [x] Known limitations — honest, matches the actual test coverage
- [x] Demo task treatment — `worker/demo_task.py` docstring now explicitly labeled as a demonstration task, not production code
- [x] Tests — 351/351 passing, 0 skipped, 0 warnings, verified against real PostgreSQL + RabbitMQ + Redis after this cleanup pass
