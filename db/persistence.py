"""Public persistence API: workflows, tasks, dependencies, and every
state transition, wrapped around ``core/transitions.py``.

Division of labor, deliberately not overlapping:

  - Postgres owns concurrency control: row locks (``FOR UPDATE`` /
    ``FOR UPDATE SKIP LOCKED``) decide who wins a race.
  - ``core/transitions.py`` (frozen, never modified) owns state-machine
    correctness: what's legal, and what the resulting field values are.

Every single-task transition follows the same shape: lock the row,
convert it to a ``TaskInstance``, call the matching ``core.transitions``
function, and only write back if it applied. ``claim_task`` and
``claim_task_by_id`` are the exception -- neither goes through
``core.transitions.claim`` at all, because doing so would require a
read-then-write round trip that reintroduces exactly the claim race
``FOR UPDATE SKIP LOCKED``/row-level locking exists to avoid. Instead each
is a single atomic UPDATE (``queries.CLAIM_READY_TASK``'s CTE+UPDATE for
the untargeted "oldest READY task" claim; ``queries.CLAIM_TASK_BY_ID``'s
plain conditional UPDATE for a targeted claim of one named task) that
encodes the identical computation (``attempt_count + 1``, lease fields)
``core.transitions.claim`` would have produced, so none of the three can
ever drift from each other as long as ``core/transitions.py`` stays
frozen. ``claim_task`` (untargeted) and ``claim_task_by_id`` (targeted)
serve different callers -- see each function's own docstring -- and
neither is a replacement for the other.

``renew_lease`` (heartbeat phase) is a fourth, differently-shaped
exception: it isn't a state transition at all in ``core/transitions.py``'s
vocabulary (state and attempt_count never change), so there is no
corresponding ``core.transitions`` function to call in the first place --
it's a single atomic conditional UPDATE touching only
``lease_expires_at``, fenced by the same ownership/state checks the
frozen module's own ``_lease_is_valid`` already encodes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from core import transitions
from core.transitions import TaskInstance, TaskState, TransitionError

from db import queries
from db.models import TaskRecord, row_to_task_instance, row_to_task_record, task_instance_update_params


class UnknownTaskError(LookupError):
    """Raised when a persistence operation targets a task_id that doesn't
    exist. This is a persistence-layer-only concern -- core/transitions.py
    has no notion of task identity at all."""


@dataclass(frozen=True)
class PersistResult:
    """Outcome of a persisted single-task transition.

    ``applied=False`` is a normal outcome (a stale worker report, a lost
    claim race, an idempotent no-op), never an exception -- mirrors
    ``core.transitions.TransitionResult`` one layer up. ``claim_task``
    is the one exception to this shape: it returns ``None`` directly
    when nothing is claimable, since "no work available" is common
    enough to want a plain falsy value rather than a wrapped result.
    """

    record: Optional[TaskRecord]
    applied: bool
    reason: Optional[str] = None


def _require_aware_utc(value: datetime, *, param_name: str) -> None:
    """Mirrors core.transitions._require_utc's rule for the one code path
    that doesn't go through a core.transitions function at all
    (claim_task, see module docstring) and therefore wouldn't otherwise
    get that validation for free. This is a pure datetime sanity check,
    not state-machine logic -- it doesn't duplicate anything that lives
    in core/transitions.py's actual decision-making (that stays solely in
    ``_resolve_after_failure`` and friends)."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise TransitionError(
            f"{param_name} must be a timezone-aware UTC datetime; got a naive datetime"
        )
    if value.utcoffset() != timedelta(0):
        raise TransitionError(
            f"{param_name} must be UTC (utcoffset=0); got offset {value.utcoffset()}"
        )


# ---------------------------------------------------------------------------
# workflows / tasks / dependencies
# ---------------------------------------------------------------------------


def _insert_ready_outbox_event(cur, row: dict) -> None:
    """Insert exactly one TASK_READY outbox event for a task row that just
    became READY, in the SAME transaction/cursor as the state write that
    produced it (see ``queries.INSERT_OUTBOX_EVENT``) -- this is what makes
    the task-state write and the outbox write atomic: one exception
    anywhere in the surrounding ``with conn.transaction():`` block rolls
    both back together, so there can never be a committed READY task
    without its event, nor a committed event for a transition that itself
    rolled back.

    ``row`` is any tasks-row dict carrying ``id``, ``workflow_id``, and
    ``task_type`` -- the payload is a dispatch hint only (task_id,
    task_type, workflow_id), never authoritative task state (no
    attempt_count, lease fields, or the task's own payload column)."""
    cur.execute(
        queries.INSERT_OUTBOX_EVENT,
        {
            "task_id": row["id"],
            "workflow_id": row["workflow_id"],
            "payload": Jsonb(
                {
                    "task_id": row["id"],
                    "task_type": row["task_type"],
                    "workflow_id": row["workflow_id"],
                }
            ),
        },
    )


def create_workflow(pool: ConnectionPool, *, name: str) -> dict:
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(queries.CREATE_WORKFLOW, {"name": name})
                row = cur.fetchone()
    return row


def create_task(
    pool: ConnectionPool,
    *,
    workflow_id: int,
    name: str,
    task_type: str,
    max_attempts: int = 1,
    payload: Optional[dict] = None,
    depends_on_ids: Optional[Sequence[int]] = None,
) -> TaskRecord:
    """Create a task and its dependency edges atomically.

    Initial state is decided by ``core.transitions.create_task`` itself
    (never reimplemented here): zero dependencies -> READY, one or more
    -> PENDING. The task row and every edge row are inserted in one
    transaction -- if any edge insert fails (e.g. a bad ``depends_on_id``,
    or the ``no_self_dependency`` constraint), the whole operation rolls
    back and no task row is left behind without its complete edge set.
    """
    depends_on_ids = list(depends_on_ids or [])
    instance = transitions.create_task(
        has_dependencies=bool(depends_on_ids), max_attempts=max_attempts
    )

    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    queries.INSERT_TASK,
                    {
                        "workflow_id": workflow_id,
                        "name": name,
                        "task_type": task_type,
                        "state": instance.state.value,
                        "max_attempts": instance.max_attempts,
                        "payload": Jsonb(payload) if payload is not None else None,
                    },
                )
                row = cur.fetchone()
                task_id = row["id"]
                for depends_on_id in depends_on_ids:
                    cur.execute(
                        queries.INSERT_TASK_DEPENDENCY,
                        {"task_id": task_id, "depends_on_task_id": depends_on_id},
                    )
                # READY-producing path 1: zero-dependency creation. Same
                # transaction as the INSERT above -- a bad dependency edge
                # below would already roll this back too, but the ordering
                # also matters: the event only exists once we're certain no
                # later statement in this transaction can still fail.
                if instance.state == TaskState.READY:
                    _insert_ready_outbox_event(cur, row)
    return row_to_task_record(row)


# ---------------------------------------------------------------------------
# claiming
# ---------------------------------------------------------------------------


def claim_task(
    pool: ConnectionPool, *, worker_id: str, now: datetime, lease_duration: timedelta
) -> Optional[TaskRecord]:
    """Atomically claim one READY task, if any exists.

    Returns ``None`` when nothing is claimable -- a common, expected
    outcome, not an error. See the module docstring for why this doesn't
    call ``core.transitions.claim`` directly -- but the two validation
    guards ``core.transitions.claim`` performs before ever touching state
    (aware-UTC ``now``, non-empty ``worker_id``) are reproduced here
    explicitly, in the same order, since nothing else would run them for
    this one code path.
    """
    _require_aware_utc(now, param_name="now")
    if not worker_id:
        raise TransitionError("worker_id is required to claim a task")
    lease_expires_at = now + lease_duration

    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    queries.CLAIM_READY_TASK,
                    {"worker_id": worker_id, "lease_expires_at": lease_expires_at},
                )
                row = cur.fetchone()

    if row is None:
        return None
    return row_to_task_record(row)


def claim_task_by_id(
    pool: ConnectionPool,
    *,
    task_id: int,
    worker_id: str,
    now: datetime,
    lease_duration: timedelta,
) -> Optional[TaskRecord]:
    """Atomically claim exactly one named task, only if it is currently
    READY (needed because a RabbitMQ dispatch hint names a specific
    ``task_id``, and the worker must never end up claiming some other task
    instead -- see ``queries.CLAIM_TASK_BY_ID``'s docstring).

    Returns ``None`` both when the task doesn't exist and when it exists
    but isn't READY -- both are "nothing to claim here" outcomes from the
    caller's point of view, and the single conditional UPDATE naturally
    collapses them into the same result without needing to distinguish
    the two. Same non-recursion-into-``core.transitions.claim`` rationale,
    and the same two validation guards reproduced explicitly, as
    ``claim_task`` above (see its docstring and the module docstring)
    -- this is a second, differently-scoped entry point onto the identical
    READY -> RUNNING computation, not a reimplementation of it.
    """
    _require_aware_utc(now, param_name="now")
    if not worker_id:
        raise TransitionError("worker_id is required to claim a task")
    lease_expires_at = now + lease_duration

    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    queries.CLAIM_TASK_BY_ID,
                    {
                        "task_id": task_id,
                        "worker_id": worker_id,
                        "lease_expires_at": lease_expires_at,
                    },
                )
                row = cur.fetchone()

    if row is None:
        return None
    return row_to_task_record(row)


def renew_lease(
    pool: ConnectionPool,
    *,
    task_id: int,
    worker_id: str,
    now: datetime,
    lease_duration: timedelta,
) -> Optional[TaskRecord]:
    """Atomically extend the lease on a task this exact worker currently
    holds (heartbeat phase prerequisite). Never touches ``state``,
    ``attempt_count``, or ``lease_owner`` -- only ``lease_expires_at``
    moves, computed as ``now + lease_duration`` (the same formula
    ``claim``/``claim_task_by_id`` already use for the initial lease, so
    there is exactly one mental model for "how long is a lease" across
    the whole system).

    Returns ``None`` -- never raises -- for every fencing failure: the
    task doesn't exist, isn't RUNNING, is held by a different worker, or
    its lease has already expired. This is a *routine, expected* outcome
    (a live worker legitimately cannot know in advance whether it's been
    fenced out -- that is exactly what this call exists to discover), so
    it mirrors ``claim_task_by_id``'s "expected outcome, not an error"
    shape rather than ``expire_lease``'s raise-based shape (whose caller,
    the reaper, is expected to have already verified the row via a locked
    call before invoking it -- a live worker calling this has no
    equivalent prior guarantee).

    The boundary is INCLUSIVE (``lease_expires_at >= now``), matching
    ``core.transitions._lease_is_valid`` (used by ``report_success``/
    ``report_failure``) -- see ``queries.RENEW_LEASE``'s comment for why.

    A database-level failure (connection error, etc.) is NOT caught here
    and propagates to the caller as an ordinary exception -- callers
    (``worker/heartbeat.py``) must treat that as transient and distinct
    from a clean ``None`` fencing result; this function does not attempt
    to make that distinction on the caller's behalf.
    """
    _require_aware_utc(now, param_name="now")
    if not worker_id:
        raise TransitionError("worker_id is required to renew a lease")
    new_lease_expires_at = now + lease_duration

    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    queries.RENEW_LEASE,
                    {
                        "task_id": task_id,
                        "worker_id": worker_id,
                        "now": now,
                        "new_lease_expires_at": new_lease_expires_at,
                    },
                )
                row = cur.fetchone()

    if row is None:
        return None
    return row_to_task_record(row)


# ---------------------------------------------------------------------------
# single-task transitions
# ---------------------------------------------------------------------------


def _load_and_lock(cur, task_id: int) -> dict:
    cur.execute(queries.SELECT_TASK_FOR_UPDATE, {"task_id": task_id})
    row = cur.fetchone()
    if row is None:
        raise UnknownTaskError(f"no task with id={task_id}")
    return row


def _write_transition(cur, task_id: int, instance: TaskInstance) -> dict:
    cur.execute(queries.UPDATE_TASK_STATE, task_instance_update_params(task_id, instance))
    return cur.fetchone()


def report_task_success(
    pool: ConnectionPool, *, task_id: int, worker_id: str, now: datetime
) -> PersistResult:
    """RUNNING -> SUCCEEDED, then (same transaction) promote any PENDING
    dependent whose dependencies are now all SUCCEEDED."""
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                row = _load_and_lock(cur, task_id)
                instance = row_to_task_instance(row)
                result = transitions.report_success(instance, worker_id=worker_id, now=now)

                if not result.applied:
                    return PersistResult(row_to_task_record(row), False, result.reason)

                new_row = _write_transition(cur, task_id, result.task)
                _propagate_ready(cur, completed_task_id=task_id)
                return PersistResult(row_to_task_record(new_row), True, None)


def report_task_failure(
    pool: ConnectionPool, *, task_id: int, worker_id: str, now: datetime
) -> PersistResult:
    """RUNNING -> READY (retry) or RUNNING -> FAILED_TERMINAL (exhausted).
    On FAILED_TERMINAL, runs the SKIPPED cascade over dependents in the
    same transaction."""
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                row = _load_and_lock(cur, task_id)
                instance = row_to_task_instance(row)
                result = transitions.report_failure(instance, worker_id=worker_id, now=now)

                if not result.applied:
                    return PersistResult(row_to_task_record(row), False, result.reason)

                new_row = _write_transition(cur, task_id, result.task)
                # READY-producing path 3: retry via report_failure (attempts
                # remain). Mutually exclusive with the FAILED_TERMINAL
                # branch below -- _resolve_after_failure always picks
                # exactly one of the two.
                if result.task.state == TaskState.READY:
                    _insert_ready_outbox_event(cur, new_row)
                elif result.task.state == TaskState.FAILED_TERMINAL:
                    _propagate_skip_cascade(cur, start_task_id=task_id)
                return PersistResult(row_to_task_record(new_row), True, None)


def expire_lease(pool: ConnectionPool, *, task_id: int, now: datetime) -> PersistResult:
    """Reaper entry point: RUNNING -> READY or FAILED_TERMINAL, driven by
    lease expiry rather than a worker report. Raises ``TransitionError``
    (and rolls back) if the task isn't RUNNING, has no lease, or the
    lease hasn't actually expired -- core.transitions.expire_lease never
    returns ``applied=False``, it either applies or raises."""
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                row = _load_and_lock(cur, task_id)
                instance = row_to_task_instance(row)
                result = transitions.expire_lease(instance, now=now)

                new_row = _write_transition(cur, task_id, result.task)
                # READY-producing path 4: retry via lease expiry (reaper
                # entry point). Same mutually-exclusive shape as
                # report_task_failure above.
                if result.task.state == TaskState.READY:
                    _insert_ready_outbox_event(cur, new_row)
                elif result.task.state == TaskState.FAILED_TERMINAL:
                    _propagate_skip_cascade(cur, start_task_id=task_id)
                return PersistResult(row_to_task_record(new_row), True, None)


def list_expired_lease_task_ids(
    pool: ConnectionPool, *, now: datetime, batch_size: int
) -> list[int]:
    """Read-only discovery for the reaper (see
    ``queries.SELECT_EXPIRED_LEASES``): RUNNING tasks whose lease has
    already expired, oldest-expiring first, batched.

    Intentionally an unlocked plain read -- it returns *candidate* ids
    only. The real correctness gate is entirely inside ``expire_lease``'s
    own per-task ``SELECT ... FOR UPDATE`` + ``core.transitions
    .expire_lease``'s re-validation; a candidate id returned here may
    already be stale by the time the reaper acts on it (a worker
    completed the task in between), which ``expire_lease`` will correctly
    reject. See ``reaper/reaper.py`` for how that expected race is
    handled.
    """
    _require_aware_utc(now, param_name="now")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.SELECT_EXPIRED_LEASES, {"now": now, "batch_size": batch_size}
            )
            rows = cur.fetchall()
    return [row["id"] for row in rows]


def mark_ready(
    pool: ConnectionPool, *, task_id: int, dependencies_satisfied: bool
) -> PersistResult:
    """PENDING -> READY. Exposed directly (in addition to being used
    internally by the propagation pass) since a caller may want to drive
    it standalone. Never recursively propagates further -- READY is not a
    cascade trigger."""
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                row = _load_and_lock(cur, task_id)
                instance = row_to_task_instance(row)
                result = transitions.mark_ready(
                    instance, dependencies_satisfied=dependencies_satisfied
                )

                if not result.applied:
                    return PersistResult(row_to_task_record(row), False, result.reason)

                new_row = _write_transition(cur, task_id, result.task)
                # mark_ready's only legal applied=True outcome is PENDING ->
                # READY (see core.transitions.mark_ready), so this is
                # unconditional here -- unlike the retry paths above, there
                # is no other resulting state to branch on.
                _insert_ready_outbox_event(cur, new_row)
                return PersistResult(row_to_task_record(new_row), True, None)


def skip(pool: ConnectionPool, *, task_id: int) -> PersistResult:
    """PENDING -> SKIPPED, then (same transaction) cascades the SKIP to
    this task's own dependents."""
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                row = _load_and_lock(cur, task_id)
                instance = row_to_task_instance(row)
                result = transitions.skip(instance)

                if not result.applied:
                    return PersistResult(row_to_task_record(row), False, result.reason)

                new_row = _write_transition(cur, task_id, result.task)
                _propagate_skip_cascade(cur, start_task_id=task_id)
                return PersistResult(row_to_task_record(new_row), True, None)


# ---------------------------------------------------------------------------
# propagation (runs inside the caller's already-open transaction/cursor)
# ---------------------------------------------------------------------------


def _propagate_ready(cur, *, completed_task_id: int) -> None:
    """Event-driven PENDING->READY check: locks every PENDING dependent of
    a just-SUCCEEDED task (ORDER BY t.id ASC, per the deadlock-avoidance
    rule) and promotes any whose dependencies are now all SUCCEEDED. Does
    not recurse -- reaching READY has no further downstream effect."""
    cur.execute(queries.LOCK_PENDING_DEPENDENTS, {"completed_task_id": completed_task_id})
    dependents = cur.fetchall()

    for dep_row in dependents:
        dep_id = dep_row["id"]
        cur.execute(queries.ALL_DEPENDENCIES_SUCCEEDED, {"task_id": dep_id})
        if not cur.fetchone()["all_satisfied"]:
            continue

        dep_instance = row_to_task_instance(dep_row)
        result = transitions.mark_ready(dep_instance, dependencies_satisfied=True)
        if result.applied:
            # READY-producing path 2: dependency propagation (fan-in).
            # Same transaction/cursor as the completion that triggered this
            # pass -- exactly one event per dependent that actually
            # transitions, never for ones already READY (mark_ready is a
            # no-op there, applied=False, and LOCK_PENDING_DEPENDENTS only
            # ever hands back PENDING rows in the first place).
            new_row = _write_transition(cur, dep_id, result.task)
            _insert_ready_outbox_event(cur, new_row)


def _propagate_skip_cascade(cur, *, start_task_id: int) -> None:
    """SKIPPED cascade: an explicit worklist (never naive recursion), with
    a ``visited`` set so a diamond-shaped DAG (two failed paths converging
    on the same downstream task) is walked at most once per node. Every
    dependent lock uses ORDER BY t.id ASC, matching the propagation pass
    and the mandatory deadlock-avoidance rule.
    """
    visited: set[int] = set()
    worklist: list[int] = [start_task_id]

    while worklist:
        current_id = worklist.pop()
        cur.execute(queries.LOCK_PENDING_DEPENDENTS, {"completed_task_id": current_id})
        dependents = cur.fetchall()

        for dep_row in dependents:
            dep_id = dep_row["id"]
            if dep_id in visited:
                continue
            visited.add(dep_id)

            dep_instance = row_to_task_instance(dep_row)
            result = transitions.skip(dep_instance)
            if result.applied:
                _write_transition(cur, dep_id, result.task)
                worklist.append(dep_id)
            # applied=False here would mean "already SKIPPED", but
            # LOCK_PENDING_DEPENDENTS only ever returns PENDING rows, so
            # in practice this branch is unreachable under the current
            # query -- `visited` is kept anyway as the mandated,
            # query-independent safety net.


# ---------------------------------------------------------------------------
# read-only queries (HTTP API prerequisite)
# ---------------------------------------------------------------------------


def get_workflow(pool: ConnectionPool, *, workflow_id: int) -> Optional[dict]:
    """A plain, unlocked read -- returns ``None`` if no workflow with this
    id exists. Never takes a row lock; safe to call concurrently with
    anything else in this module without contending for it."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(queries.SELECT_WORKFLOW_BY_ID, {"workflow_id": workflow_id})
            row = cur.fetchone()
    return dict(row) if row is not None else None


def get_task(pool: ConnectionPool, *, task_id: int) -> Optional[TaskRecord]:
    """A plain, unlocked read of one task's current row -- returns
    ``None`` if it doesn't exist. Unlike ``_load_and_lock`` (used by every
    state-transition function above), this never takes ``FOR UPDATE``: a
    status lookup must never block, or be blocked by, a concurrent claim/
    report/heartbeat/reaper transaction on the same row."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(queries.SELECT_TASK_BY_ID, {"task_id": task_id})
            row = cur.fetchone()
    if row is None:
        return None
    return row_to_task_record(row)


def list_tasks_for_workflow(pool: ConnectionPool, *, workflow_id: int) -> list[TaskRecord]:
    """Every task belonging to ``workflow_id``, oldest-created first.
    Returns an empty list both when the workflow has no tasks and when
    the workflow itself doesn't exist -- callers that need to distinguish
    "empty workflow" from "no such workflow" should call ``get_workflow``
    first, same as the API layer does (see api/routes.py)."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(queries.SELECT_TASKS_FOR_WORKFLOW, {"workflow_id": workflow_id})
            rows = cur.fetchall()
    return [row_to_task_record(row) for row in rows]


def get_workflow_task_state_counts(pool: ConnectionPool, *, workflow_id: int) -> dict:
    """Per-state task counts for one workflow, computed entirely
    in PostgreSQL (``queries.SELECT_WORKFLOW_TASK_STATE_COUNTS``) -- never
    by fetching every task row and counting in Python. A plain, unlocked
    read, same convention as ``get_task``/``list_tasks_for_workflow``: a
    status lookup must never block, or be blocked by, a concurrent claim/
    report/heartbeat/reaper transaction.

    Always returns a dict with every key present (``total_tasks=0`` for a
    workflow with no tasks) -- it cannot by itself distinguish "this
    workflow has zero tasks" from "this workflow doesn't exist"; callers
    that need that distinction must check ``get_workflow`` first (see
    ``api/routes.py``'s ``get_workflow_status``, matching the existing
    ``list_workflow_tasks`` route's pattern exactly)."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                queries.SELECT_WORKFLOW_TASK_STATE_COUNTS, {"workflow_id": workflow_id}
            )
            row = cur.fetchone()
    return dict(row)


def check_connection(pool: ConnectionPool) -> None:
    """Proves the pool can obtain a working connection and
    round-trip a query. Raises on any failure (connection refused, pool
    exhausted, etc.) -- never caught or translated here; the caller
    (``api/health.py``'s ``/readyz`` route) decides what an unreachable
    database means for its own response. Touches no table."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(queries.HEALTH_CHECK)
            cur.fetchone()
