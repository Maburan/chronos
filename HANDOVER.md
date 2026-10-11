# Chronos — Handover (for the next Claude session)

## Current Status (most recent session — autoscaling — READ THIS FIRST)

**Horizontal worker scaling and policy-based autoscaling are both
implemented, tested, and Docker-verified.** These are the two most recent
backend phases (after the frontend redesign summarized below).

**Horizontal scaling.** Multiple replicas of one worker service consume the
one `chronos.tasks.<task_type>` queue (`docker compose up -d --scale
worker-echo=3`). The only change it required was making `CHRONOS_WORKER_ID`
optional: when unset, `mq.config.derive_worker_id` derives a unique,
lifetime-stable `worker-<task_type>-<hostname>` from the container id, so
every replica has a distinct lease owner and fencing/heartbeat/reaper stay
correct. PostgreSQL's atomic claim (unchanged) is what makes N consumers on
one queue safe.

**Autoscaling** (`autoscaler/` package + `autoscaler` Compose service,
`python -m autoscaler`). A control loop that observes per-task-type backlog
and scales each worker service between MIN/MAX replicas:

- **Signal:** `READY` task count per task type from PostgreSQL
  (`db.persistence.get_backlog_counts` — new read-only query; RUNNING and
  terminal/PENDING are not backlog). No second source of truth; RabbitMQ
  depth is deliberately not used.
- **Policy** (`autoscaler/policy.py`): pure `backlog -> desired` step table,
  clamped to `[min,max]`. **Not ML.**
- **Runtime** (`autoscaler/runtime.py`): `WorkerRuntime` protocol +
  `DockerComposeRuntime` (shells `docker compose up -d --no-deps
  --no-recreate --scale worker-<type>=N` via the mounted Docker socket;
  auto-detects its own compose project). A `KubernetesRuntime` can be added
  later with no policy change.
- **Anti-thrash** (`autoscaler/autoscaler.py`): scale-up needs N consecutive
  observations (fast); scale-down needs a quiet cooldown (slow); no-op when
  `desired == current`; deduped logging.
- **Safety:** only changes replica counts — never touches task state, leases,
  or acks. Scale-down = graceful SIGTERM (worker finishes its current task;
  `stop_grace_period: 30s`) with the Reaper as backstop for hard kills.

**Key architectural decision made this phase:** worker services no longer
bind-mount `- .:/app`. They run the code **baked into the image** (`COPY .
.`). Reason: the autoscaler runs `docker compose --scale` from *inside* a
container, where a relative `.:/app` host mount does not resolve to the
source tree — scaled replicas came up with an empty `/app` and crash-looped
(`No module named worker`). Baked-in code makes a replica work regardless of
who created it. Trade-off: update worker code with `docker compose build
worker-<type> && docker compose up -d worker-<type>` (workers never
hot-reloaded anyway). api/relay/reaper/autoscaler keep their bind-mounts
(host-started, never autoscaled).

**Config** (env / `.env`, see `.env.example` + `autoscaler/config.py`):
`CHRONOS_AUTOSCALER_ENABLED` (1 in `.env` for the stack), `_INTERVAL` (10s),
`_MIN_REPLICAS` (1), `_MAX_REPLICAS` (5), `_SCALE_UP_STABILIZATION` (2),
`_SCALE_DOWN_COOLDOWN` (60s), `_BACKLOG_TIERS`, `_TASK_TYPES`,
`_SERVICE_PREFIX`, `_COMPOSE_PROJECT`.

**Files:** new `autoscaler/{__init__,config,policy,runtime,autoscaler,
__main__}.py` + `autoscaler/Dockerfile`; `db/queries.py` +
`db/persistence.py` (backlog query/fn); `docker-compose.yml` (autoscaler
service, worker `stop_grace_period`, worker bind-mounts removed);
`.env`/`.env.example`. Tests: `tests/test_autoscaler_policy.py`,
`_config.py`, `_runtime.py`, `test_autoscaler.py`, `_backlog.py` (DB),
`_integration.py` (DB). **Not modified:** `core/transitions.py`,
`db/persistence.py` transition logic, the outbox, RabbitMQ model, worker
execution.

**Verification (actual):** full suite **508 passed, 7 skipped, 0 failed**
(7 skipped = redis-limiter integration, needs `CHRONOS_TEST_REDIS_URL`);
`git diff --check` clean. Docker E2E: backlog of 40 echo tasks scaled
`worker-echo` **1→5** in ~20s, five consumers on one queue, up to **4
distinct replicas executing concurrently**, then **5→1** after the 60s
cooldown once drained; all 40 SUCCEEDED. Crash test: hard-killed a replica
mid-task → Reaper reclaimed → another replica completed it (attempt 2,
SUCCEEDED) with the autoscaler running throughout. `docker compose down`/
`up` verified. Kubernetes/Kafka/cloud provisioning explicitly NOT added.

**Note on test/Docker env on this machine:** Docker Desktop's engine pipe
for the Python SDK/testcontainers is `npipe:////./pipe/dockerDesktopLinuxEngine`
— export `DOCKER_HOST` to it if testcontainers can't reach the daemon. The
Compose broker publishes AMQP on host port **5673** (`CHRONOS_RABBITMQ_HOST_PORT`),
so broker-gated tests want
`CHRONOS_TEST_RABBITMQ_URL=amqp://chronos:chronos_dev_password@localhost:5673/`.

---

## Current Status (earlier session — frontend redesign)

**A full frontend redesign is complete and browser-verified.** The React
app was rebuilt to a polished, intentional developer-product bar
(Linear/Vercel/Raycast-inspired) without changing any backend code, API
contract, or execution/auth semantics. It was done in six deliberate
passes: (1) a cohesive design system in `frontend/src/index.css` (refined
amber-accent dark palette, type scale, elevation, motion tokens, and a
reusable primitive vocabulary — buttons/inputs/badges/cards/sheets/
tooltips/toasts/tables); (2) a consolidated single top bar
(`components/TopBar.tsx`, replacing the old two-row Header+NavBar — both
deleted); (3) dashboard + workflow list as useful, status-aware overviews;
(4) a **run-centric** workflow detail page; (5) the workflow builder as the
visual centerpiece; (6) polish, Simple/Advanced parity, states, and
verification.

**The one genuinely new capability wired in this session:** the backend
already had a complete, tested **runs API** (`POST /workflows/{id}/runs`,
`GET …/runs`, `…/runs/{rid}/status`, `…/runs/{rid}/tasks`) that the old
frontend never used. The redesign adds those functions to
`frontend/src/api.ts` and builds the whole "define → **Run** → watch live
→ understand" story on them: the detail page now clearly separates the
workflow / its definition / its execution, makes **Run workflow** the
obvious primary action, and live-polls the selected run's status + tasks
(with a run selector for history). No backend route was added or changed —
these routes already existed.

New frontend files: `components/TopBar.tsx`, `components/icons.tsx`
(dependency-free inline SVG icon set — **no icon library added**),
`components/toast.tsx`, `workflowStatus.ts`. Deleted: `components/
Header.tsx`, `components/NavBar.tsx`, and `components/PipelineVisual.tsx`
(an advanced-only static architecture diagram that didn't fit the
run-centric view and referenced the legacy task-creation path — a
deliberate removal, noted here). `vite.config.ts` gained
`server.watch.usePolling` so HMR works reliably over the Windows/Docker
bind mount (the container's inotify doesn't fire on host edits otherwise).

**No new runtime dependencies were added** (icons are hand-authored SVG;
motion is CSS). Verified this session: `tsc -b && vite build` (clean,
212 modules), `oxlint` (0 errors; only pre-existing-style advisory
warnings), `git diff --check` (clean), the full **448** backend tests
(still green — no backend files touched), and a real browser pass through
login/register, dashboard, workflow list, builder (add/configure/connect/
validate/save), detail, and a live **Run workflow** execution watched from
READY → RUNNING → SUCCEEDED, plus a FAILED run's skip-propagation and the
Simple/Advanced toggle. Responsive breakpoints (960px/720px) are in the
CSS but couldn't be screenshot-verified — the browser tool renders at a
fixed width regardless of window size; verified by code inspection only.

**A follow-up "make it feel alive" pass** then added purposeful motion
and execution visualization, all driven by real API state (never
fabricated): (1) a **live execution graph** on the detail page
(`components/execution/ExecutionGraph.tsx`, React Flow) that renders a run
as *work moving through a DAG* — state-coloured nodes, a pulsing running
node, **animated flow particles** on edges where work is actually moving
(amber), settled green paths, red dashed dead paths, dimmed skipped
nodes, smooth state-transition morphs, and click-a-node-to-inspect;
(2) **MiniDag execution strips** on dashboard/workflow-list cards
(`components/MiniDag.tsx`) showing each workflow's real latest run as
`✓─✕─○` dots + `N/M · %`; (3) builder aliveness — node entrance
animation, a validation **shake** + auto-select on save-with-incomplete,
and a technical-workspace canvas (dot grid + vignette); (4) global
micro-interactions (hover lifts, page transitions, press feedback). Pure
logic lives in `execution.ts` (topological order, per-state counts, edge
activity) and `workflowOverview.ts` (each workflow's latest-run tasks).
The old static SVG `DependencyGraph` was replaced by the live graph and
deleted. Verified live in-browser: watched real runs propagate
READY→RUNNING→SUCCEEDED and →FAILED→SKIPPED with the graph reacting in
real time; still no new dependencies, build/lint clean, animations gated
behind `prefers-reduced-motion` (and particles only render while work is
genuinely in flight).

**A composition rework** then followed (the previous passes were still a
"styled admin dashboard" — stacked bordered cards, top nav, boxed graph).
The layout was rebuilt around the idea that *the workflow canvas is the
page*: (1) the **detail page** is now a full-bleed `.canvas-page` — the
run's DAG (laid out **top-down**, work flowing downward) fills the whole
viewport as an immersive workspace (dot grid + vignette), with the page
chrome *floating* over it in frosted-glass panels: a contextual header
(breadcrumb + live status orb + tasks + a per-second elapsed timer +
progress rail), floating actions (Live/Edit/Run), a floating run selector,
and a **floating task inspector** that appears over the canvas on node
click *without dimming it* (`TaskInspectorContent.tsx`); (2) the
**builder** is the same full-bleed canvas language — floating toolbar +
floating node inspector; (3) the **dashboard** is an orchestration
overview, not a stat grid — a hero line, an **ACTIVE spotlight** of the
workflows genuinely running now (real latest-run task state, showing the
actual running task's name + a live MiniDag + %), and a lean hairline
RECENT list. The stat-card grid, the boxed "Execution graph" card, and the
steps table were removed; `MetricCards`, `TaskList`, `TaskDetailPanel`,
and the static `DependencyGraph` were deleted. Motion is meaningful and
real-data-only: flow particles animate only on edges genuinely carrying
work, running nodes pulse/breathe, the elapsed timer ticks only while a
run is in progress, all gated behind `prefers-reduced-motion`. Verified
in-browser at full desktop resolution: watched a real run flow down the
canvas READY→RUNNING→SUCCEEDED and a failing run go →FAILED→SKIPPED with
red dead-path edges; the floating inspector, dashboard ACTIVE spotlight,
and full-bleed builder all confirmed live. Build/tsc/lint/`git diff
--check` all clean; **no backend files touched, no new dependencies**.

Everything below is the prior **Phase 5** status and remains accurate.

---

## Phase 5 status (previous session)

**Phase 5 (local service orchestration / Docker Compose) is complete and
verified.** Chronos now starts with one command:

```bash
cp .env.example .env
docker compose up --build
```

This orchestrates PostgreSQL, RabbitMQ, Redis, a one-shot schema
`migrate` service (`python -m db`), the API, the outbox relay, the
reaper, one worker container per registered task type (`echo`, `sleep`,
`http_request`, `python_expression`), and the frontend dev server — as
**separate containers**, exactly mirroring the existing distributed
architecture (no service was merged into another; see
`docker-compose.yml`'s own comments for the full rationale). See
README.md's new "Docker Compose (recommended)" section under Quick Start
for the full operator-facing writeup (ports, hot reload, logs, resetting
volumes, a known-benign first-boot RabbitMQ race).

Files added: `docker-compose.yml`, root `Dockerfile` (shared by
migrate/api/relay/reaper/worker-\*), `frontend/Dockerfile`,
`reaper/__main__.py` (previously missing — see the Phase 4 session's
notes below on this exact gap), `.dockerignore`,
`frontend/.dockerignore`, `.env.example`. Nothing in
`core/transitions.py`, `db/persistence.py`, `worker/task_worker.py`,
`relay/outbox_relay.py`, or `reaper/reaper.py` was touched.

**Verified this session, for real, against a real running stack** (not
asserted from reading the compose file):
- `docker compose config` validates cleanly.
- `docker compose build` builds all 9 images successfully.
- `docker compose up -d` brings up all 12 containers; `migrate` applies
  all 4 migrations on a fresh database and exits 0; postgres/rabbitmq/redis
  all report healthy; `api` reports healthy (`/healthz` and `/readyz`
  both 200); all 4 worker containers, `relay`, and `reaper` connect and
  start consuming/polling; the frontend is reachable and serves the app.
- A real workflow + task was created through the containerized API,
  dispatched through the containerized outbox → relay → RabbitMQ chain,
  claimed and executed by the containerized `worker-echo` container, and
  reached `SUCCEEDED` in about one second — confirmed both via the API's
  own polling and by reading the literal `echo_task: ... message=...` line
  in `worker-echo`'s container log.
- `docker compose down` stops and removes all containers cleanly (no `-v`
  used); the named volumes (`postgres_data`, `rabbitmq_data`,
  `frontend_node_modules`) survive.
- `docker compose up -d` again: `migrate` correctly reports "already up
  to date" (proving idempotency), and the workflow/task created before
  `down` was still present afterward with identical data — data
  persistence across a full stack restart is confirmed, not assumed.
- The full existing pytest suite (448 tests) was run against real
  PostgreSQL/RabbitMQ/Redis **hosted inside the Docker stack** (via their
  published host ports) and passed 448/448 — no regressions introduced.

**One known, benign, cosmetic issue, not fixed by design:** on a cold
`docker compose up`, RabbitMQ's healthcheck (`rabbitmq-diagnostics -q
ping`) can report healthy a few seconds before its AMQP listener is fully
ready to accept connections. `relay` and/or one or two `worker-*`
containers may crash once with a `pika.exceptions.AMQPConnectionError`
traceback in that window; Compose's `restart: unless-stopped` policy
restarts the container automatically and it connects successfully within
~10-15 seconds. This was observed on both the initial `up` and the
restart-after-`down` in this session's verification, self-healed both
times without intervention, and does not represent a hung or broken
state — `docker compose ps` showing every service settled on "Up" (no
further restarts) is the signal startup finished. No application code
change addresses this (nor should it, per this project's own design
philosophy of tolerating exactly this class of transient failure — see
§6's fencing/retry invariants below); it could be masked with a longer
RabbitMQ healthcheck `start_period` purely at the Compose level if it
ever becomes annoying, but was left as-is since it never actually blocks
a working stack.

**Local-machine note, not a Chronos issue:** the default host ports in
`.env.example` (5432/5672/15672/6379/8000/5173) can collide with a
developer's own natively-running Postgres/RabbitMQ/Redis/Vite — this
happened on the machine this session ran on. There's no code fix for
this; if you hit a "port already in use" / bind failure on `docker
compose up`, change the colliding `*_HOST_PORT` variable(s) in your own
`.env` (not `.env.example`) to any free port — container-internal ports
never change, so nothing else needs adjusting.

**Next step:** none required — Phase 5 is done. (Note: two of the
candidates sketched here have since been built — horizontal worker scaling
via a hostname-derived `CHRONOS_WORKER_ID`, and policy-based autoscaling on
top of it; see the "autoscaling" Current Status section at the top of this
file. A production-oriented multi-stage frontend Dockerfile remains a
candidate, as does a `KubernetesRuntime` for the autoscaler.)

---

## Phase 4 summary (previous session)

- Phase 4 (frontend visual workflow builder) is implemented and
  browser-tested.
- Browser smoke testing verified: node creation/configuration, the node
  inspector, connections, cycle/self-loop rejection, node dragging,
  node/edge deletion, zoom/pan, step-picker search/categories, and
  Simple/Advanced mode.
- **Save → Reload is now VERIFIED.** `GET`/`PUT /workflows/{id}/definition`
  both succeed.
- Root cause of the earlier failure: migration
  `0004_workflow_definitions_and_runs` was missing from the active
  Chronos PostgreSQL database — the `task_definitions`,
  `task_definition_dependencies`, and `workflow_runs` tables simply
  didn't exist yet on that database. Running `python -m db` (the
  project's idempotent schema bootstrap) applied migration 0004 and
  created all three tables.
- After that, `GET`/`PUT /workflows/{id}/definition` succeeded: a real
  multi-step definition with a dependency was saved and reloaded via the
  API with the dependency edge preserved, and separately verified at the
  browser level — an Echo step was added in the builder UI, saved, the
  page reloaded, and the step reappeared correctly connected to Start.
  `/readyz` reports healthy.
- **No application code was modified** to resolve this issue — it was a
  database schema/migration state issue, not a code bug.
- The earlier "stale PostgreSQL connection pool" hypothesis from the
  prior session was **incorrect** and should not be treated as the
  current explanation for anything going forward.
- For the record, the actual infrastructure chain that had to be
  untangled this session, in order: the PostgreSQL Windows service was
  stopped; then, once started, the API turned out to be configured
  against a Postgres instance on port 5433 while a separate PostgreSQL
  service was listening on the default port 5432 (a port/instance
  mismatch, not a stale pool); and finally, once the API was correctly
  talking to the right database, the missing 0004 migration was the
  actual blocker for the definition endpoints.
- **Phase 4 is now functionally complete.**

**READ THIS SECTION FIRST.** Everything below §0 (sections 1 onward) was
written at the end of the Phase-12/publication-cleanup session, before
Chronos became multi-user. It's preserved as-is (architecture/invariants
sections are still fully accurate — nothing in this round of work touched
`core/transitions.py`, the outbox, claiming, leases, or the reaper) but
its "no auth" / "no frontend yet" framing in places is now stale; §0
below is the current state.

## 0. Multi-user evolution (this session)

Starting point: everything in §1-§25 below was already true, including
an existing React frontend (§10-13 describe it as "not started" — that
was stale even before this session; a single-page frontend already
existed). This session added, in order, without touching the frozen
state machine or any distributed-systems invariant:

1. **Auth** (`auth/`, `api/auth_routes.py`, `api/auth_dependency.py`):
   JWT bearer tokens (`HS256`, `CHRONOS_JWT_SECRET`, no default —
   `api/config.py`), bcrypt password hashing (`auth/passwords.py`, with a
   72-byte length cap matching bcrypt's own hard limit — found as a real
   bug during the Phase 8 review of this session, where a >72-byte
   password reached `bcrypt.hashpw` unguarded and raised an unhandled
   `ValueError`/500). `workflows.user_id` (new migration,
   `db/migrations/0003_users.sql`) ties every workflow to one owner;
   every workflow/task route checks ownership and 404s (never 403) for
   another user's workflow. `GET /workflows` (new) lists the caller's
   own workflows.
2. **Task types** (`worker/tasks/`): `sleep`, `http_request` (SSRF-guarded,
   documented DNS-rebinding TOCTOU gap), `python_expression` (whitelist
   AST walker, not `eval`/`exec`) alongside the original `echo`
   (`worker/demo_task.py`, untouched). A SQL task type was deliberately
   not built — see `worker/tasks/__init__.py`'s docstring for why.
   `GET /task-types` (new) serves this registry's metadata to the
   frontend.
3. **Frontend** (`frontend/src/`): added `react-router-dom` and real
   routes (`/login`, `/register`, `/` dashboard, `/workflows`,
   `/workflows/new`, `/workflows/:id`) replacing the old single-page
   "load a workflow by typing its id" UX. `AuthContext` holds the JWT.
   `DependencyGraph` now reads each task's real `depends_on_ids` (now
   returned by `GET /workflows/{id}/tasks` — see
   `db/persistence.py::list_tasks_for_workflow`) instead of a
   client-session-only approximation.
4. **Redis limiter wiring**: `worker/__main__.py` now constructs a
   `TaskTypeLimiter` when `CHRONOS_REDIS_LIMITER_ENABLED=1` is set
   (`_build_limiter`, unit-tested in isolation in
   `tests/test_worker_main_wiring.py` without needing a real worker
   loop). Previously implemented and tested but never wired into any
   entrypoint.
5. **Security review pass** (documented in README.md's "Known
   limitations" and inline docstrings): found and fixed the bcrypt
   72-byte issue above, and a **login timing side-channel** — the
   original `user is None or not verify_password(...)` short-circuited
   `verify_password` (bcrypt, ~100ms) entirely for an unknown email,
   making "unknown email" measurably faster than "known email, wrong
   password" despite an identical 401 body. Fixed in
   `api/auth_routes.py::login` by always calling `verify_password` once
   on this path (against a fixed dummy hash when there's no real user to
   check against).
6. **Demo workflows**: `scripts/seed_demo_workflows.py` creates a linear
   pipeline, a parallel diamond, and a retry demo through the real HTTP
   API (never direct DB writes).

Test count: 351/351 (the pinned baseline below) → 418 collected now, all
additions, nothing removed or weakened. The DB-backed portion was
verified against a real Postgres instance in the user's own environment
during this session (not by the agent directly, which had no reliable DB
access this session — see the session transcript for why); re-run the
full suite in your own environment before trusting this count blindly,
per this document's own standing rule (§ "before recommending from
memory" conventions apply here too).

`core/transitions.py`'s SHA-256 (§12/§25 below) was **not** recomputed
this session — no edit tool was ever opened against that file; verify it
yourself if you want the checkpoint re-confirmed.

See `README.md` for the full, up-to-date technical writeup (auth flow,
task types, Redis wiring, frontend routes) — it was updated in the same
session as this section and is the more complete reference going
forward.

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
