"""Every SQL statement the persistence layer issues, as named constants.

Nothing in ``db/persistence.py`` embeds raw SQL -- every statement lives
here, so it can be read, reviewed, and tested independently of the Python
control flow around it.
"""

CREATE_WORKFLOW = """
INSERT INTO workflows (name)
VALUES (%(name)s)
RETURNING id, name, created_at;
"""

INSERT_TASK = """
INSERT INTO tasks (workflow_id, name, task_type, state, max_attempts, payload)
VALUES (%(workflow_id)s, %(name)s, %(task_type)s, %(state)s, %(max_attempts)s, %(payload)s)
RETURNING *;
"""

INSERT_TASK_DEPENDENCY = """
INSERT INTO task_dependencies (task_id, depends_on_task_id)
VALUES (%(task_id)s, %(depends_on_task_id)s);
"""

# Atomic claim: SELECT ... FOR UPDATE SKIP LOCKED (scoped to READY rows,
# ordered so the oldest-waiting task is preferred) and the UPDATE that
# actually claims it run as one statement, in one snapshot -- there is no
# gap between "pick a row" and "claim it" for a second transaction to land
# in. Two workers running this concurrently always lock different rows;
# SKIP LOCKED is what makes that true, not application-level coordination.
CLAIM_READY_TASK = """
WITH claimed AS (
    SELECT id
    FROM tasks
    WHERE state = 'READY'
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE tasks t
SET state = 'RUNNING',
    attempt_count = t.attempt_count + 1,
    lease_owner = %(worker_id)s,
    lease_expires_at = %(lease_expires_at)s,
    updated_at = now()
FROM claimed
WHERE t.id = claimed.id
RETURNING t.*;
"""

# Targeted claim (needed because a RabbitMQ dispatch hint names a
# specific task): attempts to claim exactly one
# named task, only if it is currently READY. Unlike CLAIM_READY_TASK
# (which picks *some* claimable row), this is driven by a specific task_id
# -- e.g. a RabbitMQ dispatch hint naming task_id=42 -- and must never
# claim a different task instead. A single conditional UPDATE is both
# simpler and sufficient here: there is exactly one candidate row (by
# primary key), so there is no "which row" ambiguity for SKIP LOCKED to
# resolve. Ordinary Postgres row-level locking already gives the right
# concurrent behavior: a second transaction targeting the same task_id
# blocks briefly on the row lock, then (per Postgres's own UPDATE
# semantics) re-evaluates this WHERE clause against the now-committed row
# and simply matches zero rows if the first transaction already claimed
# it. Two transactions targeting *different* task_ids never contend at
# all. This is a READY -> RUNNING transition like CLAIM_READY_TASK, so it
# is likewise one of the transitions that does NOT produce an outbox
# event (see queries.INSERT_OUTBOX_EVENT / the outbox invariant in
# db/persistence.py).
CLAIM_TASK_BY_ID = """
UPDATE tasks
SET state = 'RUNNING',
    attempt_count = attempt_count + 1,
    lease_owner = %(worker_id)s,
    lease_expires_at = %(lease_expires_at)s,
    updated_at = now()
WHERE id = %(task_id)s
  AND state = 'READY'
RETURNING *;
"""

# Pessimistic row lock for every single-task transition (report_success,
# report_failure, expire_lease, mark_ready, skip). Postgres owns the
# concurrency control (this lock); core/transitions.py owns the resulting
# state-machine decision.
SELECT_TASK_FOR_UPDATE = """
SELECT * FROM tasks WHERE id = %(task_id)s FOR UPDATE;
"""

# Writes only the fields a TaskInstance carries, plus updated_at. Never
# touches workflow_id/name/task_type/payload/created_at.
UPDATE_TASK_STATE = """
UPDATE tasks
SET state = %(state)s,
    attempt_count = %(attempt_count)s,
    lease_owner = %(lease_owner)s,
    lease_expires_at = %(lease_expires_at)s,
    updated_at = now()
WHERE id = %(task_id)s
RETURNING *;
"""

ALL_DEPENDENCIES_SUCCEEDED = """
SELECT NOT EXISTS (
    SELECT 1 FROM task_dependencies td
    JOIN tasks t ON t.id = td.depends_on_task_id
    WHERE td.task_id = %(task_id)s AND t.state <> 'SUCCEEDED'
) AS all_satisfied;
"""

# Fan-out lookup used by both the PENDING->READY propagation pass and the
# SKIPPED cascade. ORDER BY t.id ASC before locking is mandatory: two
# concurrent completions whose fan-out sets overlap must acquire locks in
# the same global order or they can deadlock against each other.
LOCK_PENDING_DEPENDENTS = """
SELECT t.*
FROM task_dependencies td
JOIN tasks t ON t.id = td.task_id
WHERE td.depends_on_task_id = %(completed_task_id)s AND t.state = 'PENDING'
ORDER BY t.id ASC
FOR UPDATE OF t;
"""

# ---------------------------------------------------------------------------
# transactional outbox
# ---------------------------------------------------------------------------

# Inserted in the SAME transaction/cursor as the task-state write that made
# the task READY -- never as a separate commit -- so the two either both
# land or both roll back. event_type is always TASK_READY (no other event
# type is produced anywhere yet).
INSERT_OUTBOX_EVENT = """
INSERT INTO outbox_events (task_id, workflow_id, event_type, payload)
VALUES (%(task_id)s, %(workflow_id)s, 'TASK_READY', %(payload)s)
RETURNING *;
"""

# The outbox relay's read shape (oldest-unpublished-first, batched),
# defined here so that shape lives in exactly one place, and so tests can
# assert against it without hand-rolling raw SQL that could drift from what
# the relay actually runs.
SELECT_UNPUBLISHED_OUTBOX_EVENTS = """
SELECT *
FROM outbox_events
WHERE published_at IS NULL
ORDER BY created_at
LIMIT %(batch_size)s;
"""

# The relay's publish-confirmation marker. Guarded by
# ``published_at IS NULL`` so a duplicate mark (e.g. two relay passes
# racing on the same already-published row) is a harmless no-op rather
# than clobbering the original publication timestamp -- RETURNING comes
# back empty in that case, which is exactly how the relay tells "I just
# published this" apart from "this was already marked published by
# someone else". Never called until the relay has an actual publisher
# confirmation in hand; see relay/outbox_relay.py.
MARK_OUTBOX_EVENT_PUBLISHED = """
UPDATE outbox_events
SET published_at = now()
WHERE id = %(event_id)s AND published_at IS NULL
RETURNING *;
"""

# ---------------------------------------------------------------------------
# reaper
# ---------------------------------------------------------------------------

# The reaper's discovery scan: RUNNING tasks whose lease has already
# expired, oldest-expiring first, batched. Backed by idx_tasks_running_lease
# (a partial index on (lease_expires_at) WHERE state='RUNNING', already
# created in 0001_init.sql specifically for this). Deliberately a plain,
# unlocked SELECT -- it only produces a list of *candidate* task_ids. The
# actual correctness gate is entirely inside persistence.expire_lease's own
# per-task SELECT ... FOR UPDATE + core.transitions.expire_lease's validated
# re-check (state is still RUNNING, lease is still set, lease has still
# expired), called once per candidate id -- see reaper/reaper.py for why a
# stale candidate here (a worker completed the task between this scan and
# the per-task locked call) is an expected, harmless race, not a bug.
SELECT_EXPIRED_LEASES = """
SELECT id
FROM tasks
WHERE state = 'RUNNING' AND lease_expires_at < %(now)s
ORDER BY lease_expires_at
LIMIT %(batch_size)s;
"""

# ---------------------------------------------------------------------------
# heartbeat / lease renewal (heartbeat phase)
# ---------------------------------------------------------------------------

# Atomic conditional lease renewal: only the current, still-valid lease
# holder may extend its own deadline. Never touches state, attempt_count,
# or lease_owner -- only lease_expires_at (+ updated_at) moves. Mirrors
# CLAIM_TASK_BY_ID's shape exactly (a single targeted conditional UPDATE,
# no read-then-write, ordinary Postgres row-locking is sufficient for a
# single named row).
#
# The boundary is deliberately INCLUSIVE (lease_expires_at >= now), matching
# core/transitions.py's _lease_is_valid (used by report_success/
# report_failure) -- NOT expire_lease's exclusive "now > lease_expires_at"
# rule. Heartbeat is a worker-side operation like report_success/
# report_failure, not a reaper-side reclaim decision, so it uses the same
# boundary convention as those, keeping exactly one "who wins at the exact
# tick" answer (the worker) across every worker-initiated lease operation.
RENEW_LEASE = """
UPDATE tasks
SET lease_expires_at = %(new_lease_expires_at)s,
    updated_at = now()
WHERE id = %(task_id)s
  AND lease_owner = %(worker_id)s
  AND state = 'RUNNING'
  AND lease_expires_at >= %(now)s
RETURNING *;
"""

# ---------------------------------------------------------------------------
# read-only queries (HTTP API)
# ---------------------------------------------------------------------------

# Plain reads, deliberately never FOR UPDATE: a status lookup must never
# take a row lock or contend with a concurrent claim/report/heartbeat/
# reaper transaction on the same row. These are the only queries the
# API layer needs -- everything else it does is a call into an
# existing, unmodified persistence function (create_workflow, create_task).

SELECT_WORKFLOW_BY_ID = """
SELECT id, name, created_at
FROM workflows
WHERE id = %(workflow_id)s;
"""

SELECT_TASK_BY_ID = """
SELECT *
FROM tasks
WHERE id = %(task_id)s;
"""

SELECT_TASKS_FOR_WORKFLOW = """
SELECT *
FROM tasks
WHERE workflow_id = %(workflow_id)s
ORDER BY id ASC;
"""

# One aggregation query per state, computed in PostgreSQL rather
# than by fetching every task row into Python and counting there. FILTER
# lets a single pass over the (workflow_id-indexed, see idx_tasks_workflow_id)
# rows produce every per-state count at once. Always returns exactly one
# row -- count(*) is 0 for a workflow with no tasks, never NULL -- so this
# alone can't distinguish "workflow has zero tasks" from "workflow doesn't
# exist"; the caller (db.persistence.get_workflow_task_state_counts) is
# responsible for checking existence separately, same as
# SELECT_TASKS_FOR_WORKFLOW's caller already does (api/routes.py).
SELECT_WORKFLOW_TASK_STATE_COUNTS = """
SELECT
    count(*) AS total_tasks,
    count(*) FILTER (WHERE state = 'PENDING') AS pending,
    count(*) FILTER (WHERE state = 'READY') AS ready,
    count(*) FILTER (WHERE state = 'RUNNING') AS running,
    count(*) FILTER (WHERE state = 'SUCCEEDED') AS succeeded,
    count(*) FILTER (WHERE state = 'FAILED_TERMINAL') AS failed_terminal,
    count(*) FILTER (WHERE state = 'SKIPPED') AS skipped
FROM tasks
WHERE workflow_id = %(workflow_id)s;
"""

# ---------------------------------------------------------------------------
# health check (operational readiness)
# ---------------------------------------------------------------------------

# Touches no table -- just proves the pool can obtain a working connection
# and round-trip a query. Used by db.persistence.check_connection, which
# api/health.py's /readyz route calls.
HEALTH_CHECK = "SELECT 1;"
