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
It is fully implemented and tested against a real Redis instance, and is
now wired into the `python -m worker` entrypoint behind an explicit
opt-in flag: set `CHRONOS_REDIS_LIMITER_ENABLED=1` (plus
`CHRONOS_REDIS_URL`) to enable it for that worker process; omit it and
behavior is exactly as before (no limiter, unlimited concurrency). See
`worker/__main__.py::_build_limiter`.

### Authentication and workflow ownership

Every workflow/task route requires a JWT bearer token (`POST /auth/register`,
`POST /auth/login`, `POST /auth/logout`, `GET /auth/me` — see
`api/auth_routes.py`). Passwords are hashed with bcrypt (`auth/
passwords.py`); tokens are stateless `HS256` JWTs signed with
`CHRONOS_JWT_SECRET` (`auth/tokens.py`) — there is no server-side session
table, so `/auth/logout` drops the frontend's locally-stored token but
cannot revoke it early. Every workflow belongs to exactly one user
(`workflows.user_id`); every route that reads or mutates a specific
workflow (`GET/POST /workflows/{id}...`, `GET /tasks/{id}`) checks
ownership and returns a plain 404 — never 403 — for a workflow that
either doesn't exist or belongs to someone else, so a caller can't
distinguish the two. `GET /workflows` lists only the caller's own
workflows. `core/transitions.py`, the outbox, claiming, leases, and the
reaper are entirely unaware that authentication exists — ownership is
checked once, at the HTTP boundary, before any persistence call.

### Task types

`worker/tasks/` registers four task types (`worker/tasks/__init__.py`'s
`TASK_TYPE_REGISTRY`, also served as metadata via `GET /task-types` for
the frontend's task-type picker):

- **`echo`** — the original demonstration task (`worker/demo_task.py`,
  unchanged); logs a message, with `fail_until_attempt`/`sleep_seconds`
  knobs for demoing retries/crash recovery.
- **`sleep`** — blocks for `payload.seconds`, capped at 300s.
- **`http_request`** — one outbound HTTP call, with SSRF guardrails
  (private/loopback/link-local/metadata addresses blocked by default,
  redirects never auto-followed, bounded timeout, capped/streamed
  response body — see `worker/tasks/http_task.py`'s module docstring,
  including an honestly-documented DNS-rebinding TOCTOU limitation that
  isn't fully closed).
- **`python_expression`** — evaluates a whitelist-only restricted
  arithmetic/comparison expression via a hand-rolled AST walker
  (`worker/tasks/python_task.py`) — explicitly **not** `eval`/`exec` or
  general-purpose code execution; no imports, attribute access, or
  non-whitelisted function calls are reachable.
- **SQL query task type: deliberately not implemented.** Documented in
  `worker/tasks/__init__.py`'s module docstring — arbitrary user-supplied
  SQL against a shared credential has no safe sandbox in this
  architecture.

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

### Docker Compose (recommended)

The fastest way to run the complete stack locally — no manually-managed
PostgreSQL/RabbitMQ/Redis, no juggling five terminal windows. This
orchestrates the *same* distributed processes described throughout this
README as separate containers; it does not merge them into one process
(see `docker-compose.yml`'s own comments and HANDOVER.md's Phase 5
section for the full design rationale).

**Prerequisites:** Docker Desktop (or another Docker Engine + Compose v2
install) running.

```bash
# 1. Copy the example environment file and adjust if you like (the
#    defaults are safe, local-only placeholder credentials -- see
#    .env.example's own comments). Never commit the real .env.
cp .env.example .env

# 2. Build and start everything: PostgreSQL, RabbitMQ, Redis, a one-shot
#    schema migration, the API, the outbox relay, the reaper, one worker
#    per registered task type (echo/sleep/http_request/python_expression),
#    and the frontend dev server.
docker compose up --build
```

That's it. Once every container reports healthy/started (`docker compose
ps` shows this), Chronos is reachable at:

- **Frontend:** http://localhost:5173 (or whatever `CHRONOS_FRONTEND_HOST_PORT`
  is set to in your `.env`)
- **API:** http://localhost:8000 (`CHRONOS_API_HOST_PORT`) — try
  `curl http://localhost:8000/healthz`
- **RabbitMQ management UI:** http://localhost:15672
  (`CHRONOS_RABBITMQ_MANAGEMENT_HOST_PORT`), login with the
  `CHRONOS_RABBITMQ_USER`/`CHRONOS_RABBITMQ_PASSWORD` from your `.env`

If any of those ports are already in use on your machine (e.g. a native
Postgres/RabbitMQ/Redis install, or another project), change the
corresponding `*_HOST_PORT` variable in `.env` — only the host-published
port changes; containers still talk to each other over the internal
Docker network using the standard ports.

**Why a `migrate` service exists:** it runs `python -m db` (the same
idempotent schema bootstrap `python -m db` always was) once, and every
other backend service waits for it to *successfully complete* before
starting (`depends_on: migrate: condition: service_completed_successfully`
in `docker-compose.yml`). This makes the exact bug a previous development
session spent hours diagnosing — a backend process starting against a
database missing a migration — structurally impossible: nothing can start
against a stale schema again.

**Hot reload:** the API container runs `uvicorn ... --reload` against a
bind-mounted copy of the repo, and the frontend container runs Vite's own
dev server the same way — edit code on your host, both pick it up
automatically. The relay/reaper/worker-\* containers are plain blocking
loops with no reload machinery of their own (same as running them outside
Docker); after editing their code, restart just that service:

```bash
docker compose restart relay
docker compose restart worker-echo   # or worker-sleep / worker-http_request / worker-python_expression
docker compose restart reaper
```

**Logs:**

```bash
docker compose logs -f                 # everything, follow mode
docker compose logs -f api             # one service
docker compose logs --tail 50 worker-echo
```

**Stopping:**

```bash
docker compose down        # stops and removes containers; PostgreSQL/RabbitMQ
                            # data (named volumes) is preserved
```

**Resetting the database/volumes entirely** (e.g. to start from a
genuinely empty Postgres, or if you've made a migration mistake locally):

```bash
docker compose down -v     # also removes postgres_data/rabbitmq_data volumes
docker compose up --build  # migrate runs against a fresh, empty database again
```

**A first-boot quirk worth knowing about, not worrying about:** on a cold
`docker compose up`, the RabbitMQ container can report itself healthy a
few seconds before its AMQP listener is fully ready to accept
connections. If you see `relay`/`worker-*` briefly crash with a
`pika.exceptions.AMQPConnectionError` traceback in the logs right after
startup, that is expected — Compose's `restart: unless-stopped` policy
restarts that one container automatically, and it connects successfully
on the next attempt (typically within 10-15 seconds total). This is the
same category of transient failure the reaper/crash-recovery design
throughout this README is built to tolerate, just observed at container
startup instead of mid-run. No code change fixes this because nothing is
actually broken; `docker compose ps` settling on every service "Up" (no
further restarts) is the signal that startup is complete.

**Verifying an end-to-end run** through the Docker stack (same shape as
the manual walkthrough below, just against the containerized API):

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "demo@chronos.local", "password": "chronos-demo-password"}' \
  > /dev/null; curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "demo@chronos.local", "password": "chronos-demo-password"}' \
  | python -c "import sys, json; print(json.load(sys.stdin)['access_token'])")

WFID=$(curl -s -X POST http://localhost:8000/workflows \
  -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d '{"name": "docker-smoke-test"}' | python -c "import sys, json; print(json.load(sys.stdin)['id'])")

curl -s -X POST http://localhost:8000/workflows/$WFID/tasks \
  -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d '{"name": "t1", "task_type": "echo", "payload": {"message": "hello from docker"}}'

curl -s http://localhost:8000/workflows/$WFID/status -H "Authorization: Bearer $TOKEN"
# -> should reach {"is_complete": true, "succeeded": 1, "has_failures": false, ...}
# within a second or two once the relay/RabbitMQ/worker-echo chain runs it.
```

## Horizontal scaling and autoscaling

Chronos scales workers along one axis: **more replicas of the same task
type, all consuming the same queue.** Both the manual and the automatic
form of that are implemented.

```
task type  ->  one durable RabbitMQ queue  ->  N worker replicas
                                                     ^
                                                     | desired replica count
                                          Autoscaler (autoscaler/)
```

### Horizontal scaling (implemented)

Every worker service can run with multiple replicas that all consume the
one `chronos.tasks.<task_type>` queue — never a queue per replica:

```bash
docker compose up -d --scale worker-echo=3   # three echo workers, one queue
```

Each replica gets a **unique, lifetime-stable identity** even though they
share one service definition and one env: when `CHRONOS_WORKER_ID` is
unset, `mq.config.derive_worker_id` derives `worker-<task_type>-<hostname>`
from the container hostname (its container id). Unique identity is what
keeps lease ownership/fencing, heartbeats, and reaper recovery correct
under N replicas. Safe concurrency is enforced entirely by PostgreSQL's
atomic claim (`SELECT ... FOR UPDATE SKIP LOCKED` / the targeted
conditional `UPDATE`) — RabbitMQ only distributes hints; two workers
consuming the same queue can never both execute the same task.

### Autoscaling (implemented)

**What it is.** A dedicated control-loop process (`python -m autoscaler`,
the `autoscaler` Compose service) that periodically observes each task
type's backlog and adjusts that task type's worker replica count between a
configured min and max. It is **policy-based, not ML-based**, and is
**Docker Compose / local-runtime oriented** — see limitations below.

**How it works (architecture).** Three cleanly separated layers, each
independently unit-tested:

- **Signal — PostgreSQL, the source of truth.** Backlog is the count of
  `READY` tasks per task type (`db.persistence.get_backlog_counts`, a
  plain unlocked read). `RUNNING` tasks (already being executed) and
  terminal/`PENDING` tasks are **not** counted as backlog. No second
  source of truth is introduced; RabbitMQ queue depth is deliberately
  *not* used, because in Chronos a queued message only means "maybe
  claimable", never authoritative state.
- **Policy — `autoscaler/policy.py`.** A pure function `backlog ->
  desired replicas`, then clamped to `[min, max]`. Configurable step
  table; the default:

  | backlog | desired replicas |
  |--------:|-----------------:|
  |   0–2   |        1         |
  |   3–5   |        2         |
  |   6–10  |        3         |
  |  11–20  |        4         |
  |   21+   |        5         |

- **Actuator — `autoscaler/runtime.py`.** The loop depends only on a
  `WorkerRuntime` abstraction (`current_replicas`, `scale`). The one
  implementation, `DockerComposeRuntime`, maps a task type to its Compose
  service (`worker-<task_type>`) and runs
  `docker compose up -d --no-deps --no-recreate --scale worker-<type>=N
  worker-<type>` against the daemon (via the mounted Docker socket). A
  future `KubernetesRuntime` would implement the same two methods with
  zero change to the policy or the loop.

**Anti-thrash.** The reconcile loop (`autoscaler/autoscaler.py`) is
deliberately asymmetric — **scale up fast, scale down slow**:

- *Scale-up stabilization*: the higher target must hold for
  `SCALE_UP_STABILIZATION` consecutive observations before scaling up (so
  a one-cycle blip is ignored, but a real spike is handled within a couple
  of intervals).
- *Scale-down cooldown*: the queue must stay quiet for
  `SCALE_DOWN_COOLDOWN` seconds before scaling down (so a queue that
  briefly empties keeps its workers).
- No runtime call at all when `desired == current`, and steady-state /
  cooldown states are logged only when they change — no per-cycle spam.

Each task type carries its own independent stabilization/cooldown state.

**Safety.** Autoscaling only ever changes a replica *count*. It never
touches task state, never claims/acks, never manipulates a lease. Scaling
a worker down is a plain Compose stop: the worker handles `SIGTERM` by
letting its current task finish (within `stop_grace_period: 30s`) and then
exiting; a task that outlives the grace period is hard-killed and left
`RUNNING` for the **Reaper** to reclaim — the exact crash-recovery path
that already exists and is tested. No task is lost or executed twice
because scaling happened.

**Configuration** (all via env / `.env`, documented in `.env.example`;
same `from_env()` convention as the rest of Chronos — see
`autoscaler/config.py`):

| Variable | Default | Meaning |
|---|---|---|
| `CHRONOS_AUTOSCALER_ENABLED` | `false` | Master switch (set `1` in `.env` for the stack) |
| `CHRONOS_AUTOSCALER_INTERVAL` | `10` | Seconds between observations |
| `CHRONOS_AUTOSCALER_MIN_REPLICAS` | `1` | Lower clamp |
| `CHRONOS_AUTOSCALER_MAX_REPLICAS` | `5` | Upper clamp |
| `CHRONOS_AUTOSCALER_SCALE_UP_STABILIZATION` | `2` | Consecutive observations before scale-up |
| `CHRONOS_AUTOSCALER_SCALE_DOWN_COOLDOWN` | `60` | Quiet seconds before scale-down |
| `CHRONOS_AUTOSCALER_BACKLOG_TIERS` | `0:1,3:2,6:3,11:4,21:5` | Backlog→replica step table |
| `CHRONOS_AUTOSCALER_TASK_TYPES` | `CHRONOS_TASK_TYPES` | Which task types to manage |
| `CHRONOS_AUTOSCALER_SERVICE_PREFIX` | `worker-` | `service = prefix + task_type` |
| `CHRONOS_AUTOSCALER_COMPOSE_PROJECT` | auto-detect | Compose project to scale |

**How to observe it.** The autoscaler logs decisions (and only decisions,
not every idle cycle):

```
[autoscaler] echo backlog=8 current=1 desired=3
[autoscaler] scaling worker-echo 1 -> 3
...
[autoscaler] echo backlog=0 current=3 desired=1 scale-down cooldown active (24s/60s)
[autoscaler] scaling worker-echo 3 -> 1 (cooldown elapsed)
```

```bash
docker compose logs -f autoscaler          # scaling decisions
docker compose ps worker-echo               # current replica count
docker exec chronos-rabbitmq-1 rabbitmqctl list_queues name consumers   # consumers on each queue
```

**Running a scaling demo:**

```bash
docker compose up -d --build          # autoscaler enabled by default via .env; starts 1 of each worker
# submit a burst of long-ish echo tasks (auth as in the end-to-end snippet above), e.g. 30 tasks with
#   payload {"message": "x", "sleep_seconds": 8}
# then watch:
docker compose logs -f autoscaler     # observe backlog rise -> "scaling worker-echo 1 -> 5"
docker compose ps worker-echo         # replica count climbs to MAX
# let the queue drain, wait out the cooldown, and watch it scale back to MIN:
#   "scaling worker-echo 5 -> 1 (cooldown elapsed)"
```

*(An observed run: backlog of 40 echo tasks scaled `worker-echo` 1→5 within
~20s, five consumers shared the one queue with up to four replicas
executing concurrently, then it scaled 5→1 after the 60s cooldown once the
queue drained — all 40 tasks SUCCEEDED. Killing a replica mid-task during
this was recovered by the Reaper and completed by another replica, with the
autoscaler running throughout.)*

### Current limitations

- **Local runtime only.** Only `DockerComposeRuntime` exists.
  **Kubernetes / cloud autoscaling is NOT implemented**, and there is **no
  automatic cloud resource provisioning** — the autoscaler adjusts replica
  counts of already-defined Compose services on one host; it never
  provisions machines. The `WorkerRuntime` seam exists so a
  `KubernetesRuntime` can be added later without touching the policy.
- **Policy-based, not ML-based.** Fixed step-table thresholds; no
  prediction, no learning.
- Backlog uses only `READY` count today. The policy is a pure function so
  an additional signal (e.g. oldest-queued-task age) can be folded in
  later without touching the runtime or the loop; it is intentionally left
  out of the first version.
- The autoscaler drives Compose via the host Docker socket it mounts —
  appropriate for local/dev, not a hardened multi-tenant control plane.
- With the default per-worker `prefetch` of 10, a single worker that is
  already consuming can buffer several dispatch hints locally, so a burst
  submitted *before* scale-up may be partly drained by the first replica
  rather than perfectly spread; scaling still adds real concurrent
  capacity. Lower `CHRONOS_WORKER_PREFETCH` (e.g. to 1) for long tasks if
  you want the tightest possible spread.

### Manual (without Docker)

Chronos assumes PostgreSQL, RabbitMQ, and (optionally) Redis are already
running and reachable — however you run them locally is up to you; Chronos
itself makes no assumption beyond a working connection string for each.

```bash
# Postgres: create an (empty) database for Chronos to use.
createdb chronos

# Environment variables Chronos reads (see mq/config.py, api/config.py):
export CHRONOS_DATABASE_URL="postgresql://postgres:<password>@localhost:5432/chronos"
export CHRONOS_JWT_SECRET="<a long random string -- required, no default>"
export CHRONOS_RABBITMQ_URL="amqp://guest:guest@localhost:5672/"
export CHRONOS_TASK_TYPES="echo"          # declared by the relay at startup
export CHRONOS_WORKER_ID="worker-1"
export CHRONOS_WORKER_TASK_TYPE="echo"
# Optional: enable the Redis admission limiter for this worker.
# export CHRONOS_REDIS_LIMITER_ENABLED="1"
# export CHRONOS_REDIS_URL="redis://localhost:6379/0"

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

With all four running, register a user, log in, create a workflow and a
task, then poll its status. Every workflow/task route requires the bearer
token from login (see "Authentication and workflow ownership" above):

```bash
# Register, then log in to get a bearer token.
curl -s -X POST http://127.0.0.1:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "demo@chronos.local", "password": "chronos-demo-password"}'

TOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "demo@chronos.local", "password": "chronos-demo-password"}' \
  | python -c "import sys, json; print(json.load(sys.stdin)['access_token'])")

# Create a workflow.
curl -s -X POST http://127.0.0.1:8000/workflows \
  -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d '{"name": "demo"}'
# -> {"id": 1, "name": "demo", "user_id": 1, "created_at": "..."}

# Create a task in it (task_type "echo" is the original demo task type --
# see worker/demo_task.py; worker/tasks/ also registers sleep,
# http_request, and python_expression -- see GET /task-types).
curl -s -X POST http://127.0.0.1:8000/workflows/1/tasks \
  -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d '{"name": "t1", "task_type": "echo", "payload": {"message": "hello"}}'
# -> {"id": 1, "state": "READY", ...}

# Poll the workflow's rollup status until it's complete.
curl -s http://127.0.0.1:8000/workflows/1/status -H "Authorization: Bearer $TOKEN"
# -> {"workflow_id": 1, "total_tasks": 1, "succeeded": 1, ...,
#     "is_complete": true, "has_failures": false}
```

Alternatively, seed a few more realistic example workflows (a linear
pipeline, a parallel diamond, and a retry demo) through the same public
API with `python -m scripts.seed_demo_workflows` (see `scripts/
seed_demo_workflows.py` — registers/logs in a demo user itself).

## Frontend / Dashboard

`frontend/` is a React + TypeScript + Vite dashboard that exercises the
same HTTP API above — it has no privileged access, no direct database or
broker connection, and adds no server-side behavior. It exists to make
the execution flow (`PENDING/READY -> RUNNING -> SUCCEEDED /
FAILED_TERMINAL / SKIPPED`) visible without reading logs or writing curl
commands by hand.

Routes (React Router, `frontend/src/App.tsx`):

- **`/login`, `/register`** — email/password forms against `POST
  /auth/login` / `POST /auth/register`; the JWT is stored in
  `localStorage` (`frontend/src/api.ts`) and attached as an
  `Authorization` header on every subsequent request
  (`frontend/src/auth/AuthContext.tsx`).
- **`/`** — dashboard: total/running/completed/failed workflow counts
  (derived from `GET /workflows` + one `GET /workflows/{id}/status` per
  workflow, run in parallel — there is no dedicated dashboard-aggregation
  endpoint) and a recent-workflows list.
- **`/workflows`** — the caller's own workflows (`GET /workflows`, added
  specifically for this frontend work — see `db/persistence.py::
  list_workflows_for_user`).
- **`/workflows/new`** — creates a workflow, then hands off to its detail
  page; there is no separate "draft" state — each task added there is
  already a real, persisted `POST /workflows/{id}/tasks` call.
- **`/workflows/:id`** — task creation (task-type picker sourced from
  `GET /task-types`, dependency picker, JSON payload editor with a
  generated template), a live task table (2-second polling — there is no
  push/WebSocket channel in the API), a task detail panel (attempt
  counts, lease owner/expiry, full payload, a lifecycle stepper derived
  from the task's *current* state, not a fabricated history), and a
  dependency graph now built directly from each task's own
  `depends_on_ids` (returned by `GET /workflows/{id}/tasks` — no longer a
  client-session-only approximation).
- A static pipeline diagram (API -> Postgres/Outbox -> Outbox Relay ->
  RabbitMQ -> Worker -> Task Execution) and a one-click retry-demo helper
  (`fail_until_attempt`) are unchanged from the original single-page
  version.

The frontend requires CORS enabled on the API for its own origin
(`http://localhost:5173` by default — see `api/config.py`'s
`cors_origins` / `CHRONOS_API_CORS_ORIGINS`) and otherwise makes no
changes to backend behavior. Bearer-token auth (not cookies) means CSRF
doesn't apply to these requests; the token is `localStorage`-resident, so
the usual XSS-exposes-the-token tradeoff applies (mitigated by a
reasonably short default JWT expiry, `CHRONOS_JWT_EXPIRY_MINUTES`).

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
  -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d '{"name": "crash-demo", "task_type": "echo", "max_attempts": 2,
       "payload": {"message": "will survive a crash", "sleep_seconds": 20}}'
```

**3. Watch it become `RUNNING`:**

```bash
curl -s http://127.0.0.1:8000/tasks/2 -H "Authorization: Bearer $TOKEN"
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
curl -s http://127.0.0.1:8000/tasks/2 -H "Authorization: Bearer $TOKEN"
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
curl -s http://127.0.0.1:8000/tasks/2 -H "Authorization: Bearer $TOKEN"
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
curl -s http://127.0.0.1:8000/tasks/2 -H "Authorization: Bearer $TOKEN"
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

**418 tests collected** (up from the earlier 351/351 baseline — the
difference is entirely new coverage added for auth/authorization, the
new task types, `GET /task-types`, and the Redis-limiter wiring; no
existing test was deleted or weakened). Run against real PostgreSQL, real
RabbitMQ, and real Redis simultaneously for the full integration/
end-to-end suite (no service is mocked there); unit tests for pure logic
— e.g. `core/transitions.py`, `worker/tasks/*` — use no infrastructure at
all, by design.

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

- **No password reset, email verification, or server-side token
  revocation.** Auth is deliberately minimal — register/login/logout/me
  only. A stolen JWT is valid until it expires; there is no way to force
  it invalid earlier short of rotating `CHRONOS_JWT_SECRET` (which
  invalidates *every* issued token, not just one).
- **`http_request`'s SSRF guard has a DNS-rebinding TOCTOU gap.** The
  hostname is resolved and checked once before the request; the actual
  outbound connection resolves DNS again. See `worker/tasks/http_task.py`'s
  module docstring.
- **Workflow/task name length is unbounded at the API layer.** Pre-existing,
  not introduced by this round of work — `CreateWorkflowRequest.name`/
  `CreateTaskRequest.name` have no `max_length`.
- **The dashboard's workflow counts cost one `GET /status` call per
  workflow** (run in parallel, but still O(n) requests) — fine at demo
  scale, not the shape you'd want for hundreds of workflows.
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
- **The fallback poller has no standalone entrypoint.** Fully implemented
  and tested, but using it today means writing a small amount of wiring
  code yourself (see `worker/fallback_poller.py`) rather than running a
  shipped command. (The Redis limiter now does have a wired-in entrypoint
  — see above.)

---

## Publication checklist

- [x] README — this file
- [x] Architecture diagram — Mermaid diagram + explicit execution-flow list
- [x] Quick Start — accurate env vars/commands/ports, taken directly from the current code
- [x] Crash Recovery Demo — step-by-step, using only existing repository mechanisms
- [x] Known limitations — honest, matches the actual test coverage
- [x] Demo task treatment — `worker/demo_task.py` docstring now explicitly labeled as a demonstration task, not production code
- [x] Tests — 351/351 passing, 0 skipped, 0 warnings, verified against real PostgreSQL + RabbitMQ + Redis after this cleanup pass
