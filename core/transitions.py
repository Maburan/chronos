"""Chronos task state-transition module.

This module owns the finite-state-machine rules for a single Chronos task's
lifecycle. It is intentionally narrow in scope: it knows nothing about the
database, HTTP layer, scheduler loop, worker processes, Redis, migrations,
the reaper process itself, or DAG validation. Callers are responsible for:

  - deciding *when* to invoke a transition (e.g. the scheduler decides a
    dependency graph says a task's dependencies are all SUCCEEDED, and
    passes that boolean in),
  - persisting the resulting ``TaskInstance``,
  - driving an actual clock and detecting expired leases (the reaper loop
    itself), then calling ``expire_lease`` with the result.

Persisted states
-----------------
PENDING, READY, RUNNING, SUCCEEDED, FAILED_TERMINAL, SKIPPED.

Note that ``FAILED`` is *not* a persisted state. A failed attempt resolves
immediately, within the same transition, to either READY (a retry is
available) or FAILED_TERMINAL (attempts are exhausted).

Design notes
------------
Every transition function is a pure function: it takes an immutable
``TaskInstance`` (and any inputs needed to decide the transition) and
returns a ``TransitionResult`` wrapping either the unchanged input (the
transition did not apply) or a new ``TaskInstance`` (the transition
applied). Nothing is mutated in place, which makes the rules easy to unit
test and easy for a caller to persist atomically (e.g. "compare-and-swap
on the row version").

Two different failure modes are treated differently, on purpose:

  * ``claim()`` treats an invalid source state (task not READY) as a
    caller/DB-contract violation: whatever query handed this task to a
    worker as claimable should already have filtered to READY, so landing
    here otherwise means that contract was violated upstream. It reports
    this as ``applied=False`` (not an exception) because losing a claim
    race to another worker is an expected, frequent outcome of normal
    concurrent operation -- not something callers should have to wrap in
    try/except on every claim attempt.

  * ``report_success()`` / ``report_failure()`` treat a report from a
    worker that no longer owns the active lease (wrong worker, expired
    lease, task already resolved) as an *expected runtime race*: workers
    can be slow, retried, or zombied, and their reports can arrive after
    the fact. These are "safely ignored" -- ``applied=False``, task
    unchanged, no exception -- because a crash here would let a stale
    worker's report take down the reporting path for everyone.

  * ``mark_ready()`` and ``skip()`` raise ``TransitionError`` for any
    source state that is not their documented legal source (PENDING) or
    idempotent no-op target (READY / SKIPPED respectively), since these
    are driven by a single scheduler decision process, not by racing
    workers, and an unexpected source state there signals a genuine
    caller bug (e.g. operating on the wrong task, or a DAG-evaluation
    error) rather than a routine race.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Optional


class TaskState(str, enum.Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    SKIPPED = "SKIPPED"


TERMINAL_STATES = frozenset(
    {TaskState.SUCCEEDED, TaskState.FAILED_TERMINAL, TaskState.SKIPPED}
)


class TransitionError(Exception):
    """Raised when a transition is requested from a structurally illegal state,
    or with a malformed input (e.g. a naive datetime).

    This signals a caller/orchestrator bug, not an expected runtime race.
    Expected races (stale worker reports, duplicate cascades, lost claims)
    are reported via ``TransitionResult.applied is False`` instead.
    """


def _require_utc(value: datetime, *, param_name: str) -> None:
    """Reject any datetime that is not timezone-aware UTC.

    Naive datetimes are never silently assumed to be UTC -- they are
    rejected outright, since guessing wrong here can corrupt lease-fencing
    comparisons in ways that are hard to notice later.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise TransitionError(
            f"{param_name} must be a timezone-aware UTC datetime; got a naive datetime"
        )
    if value.utcoffset() != timedelta(0):
        raise TransitionError(
            f"{param_name} must be UTC (utcoffset=0); got offset {value.utcoffset()}"
        )


@dataclass(frozen=True)
class TransitionResult:
    """Outcome of attempting a transition.

    ``task`` is always populated: on success it is the new post-transition
    instance, on a no-op it is the original, unchanged instance. ``reason``
    is a short human-readable explanation, only present when ``applied``
    is False.
    """

    task: "TaskInstance"
    applied: bool
    reason: Optional[str] = None


@dataclass(frozen=True)
class TaskInstance:
    """Immutable snapshot of a single task's state-machine-relevant fields.

    Dependency satisfaction is supplied by the caller at the moment of
    each ``mark_ready`` call (this module does not know about, or cache
    anything about, the DAG) -- it only knows how to react to
    "dependencies satisfied: yes/no".
    """

    state: TaskState
    attempt_count: int = 0
    max_attempts: int = 1
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.attempt_count < 0:
            raise ValueError("attempt_count cannot be negative")
        if self.attempt_count > self.max_attempts:
            raise ValueError("attempt_count must never exceed max_attempts")
        if (self.lease_owner is None) != (self.lease_expires_at is None):
            raise ValueError(
                "lease_owner and lease_expires_at must be treated as a pair: "
                "both set or both None"
            )
        if self.state == TaskState.RUNNING and self.lease_owner is None:
            raise ValueError("a RUNNING task must hold an active lease")
        if self.state != TaskState.RUNNING and self.lease_owner is not None:
            raise ValueError("only a RUNNING task may hold a lease")
        if self.lease_expires_at is not None:
            _require_utc(self.lease_expires_at, param_name="lease_expires_at")

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def attempts_exhausted(self) -> bool:
        return self.attempt_count >= self.max_attempts


def _resolve_after_failure(task: TaskInstance) -> TaskState:
    """Shared retry-vs-terminal decision, used by both a worker-reported
    failure (``report_failure``) and a reaper-detected lease expiry
    (``expire_lease``), so the rule lives in exactly one place.

    attempts remaining -> READY; attempts exhausted -> FAILED_TERMINAL.
    """
    return TaskState.FAILED_TERMINAL if task.attempts_exhausted else TaskState.READY


def create_task(*, has_dependencies: bool, max_attempts: int = 1) -> TaskInstance:
    """Create a brand-new task.

    - creation with no dependencies -> READY
    - creation with dependencies -> PENDING

    ``has_dependencies`` is used only to pick the initial state; it is not
    stored on the resulting ``TaskInstance`` (this module does not cache
    DAG facts -- the DAG itself is the source of truth for that, owned
    elsewhere).
    """
    state = TaskState.PENDING if has_dependencies else TaskState.READY
    return TaskInstance(state=state, max_attempts=max_attempts)


def mark_ready(task: TaskInstance, *, dependencies_satisfied: bool) -> TransitionResult:
    """PENDING -> READY once all dependencies are SUCCEEDED.

    - already READY -> safe no-op (duplicate dependency-resolution events
      are expected from the scheduler and must not raise).
    - PENDING with dependencies_satisfied -> READY.
    - PENDING without dependencies_satisfied -> no-op, stays PENDING.
    - any other state -> TransitionError (caller bug; see module docstring).

    The caller (scheduler) is responsible for actually evaluating the DAG
    and passing the result in as ``dependencies_satisfied``.
    """
    if task.state == TaskState.READY:
        return TransitionResult(task, False, "already READY; no-op")
    if task.state != TaskState.PENDING:
        raise TransitionError(
            f"cannot mark ready: task is {task.state}, not PENDING"
        )
    if not dependencies_satisfied:
        return TransitionResult(task, False, "dependencies not yet satisfied")
    return TransitionResult(replace(task, state=TaskState.READY), True)


def claim(
    task: TaskInstance, *, worker_id: str, now: datetime, lease_duration: timedelta
) -> TransitionResult:
    """READY -> RUNNING: a worker atomically claims the task.

    This is where attempt_count is incremented -- exactly once, exactly
    here, and nowhere else in the module.

    Invalid source state (not READY) is a caller/DB-contract violation in
    principle (see module docstring), but is reported as ``applied=False``
    rather than raised, because losing a claim race to another worker
    (task no longer READY by the time this runs) is an expected, routine
    outcome of concurrent operation that callers must handle on every
    claim attempt anyway.
    """
    _require_utc(now, param_name="now")
    if not worker_id:
        raise TransitionError("worker_id is required to claim a task")
    if task.state != TaskState.READY:
        return TransitionResult(task, False, f"cannot claim: task is {task.state}, not READY")
    if task.attempts_exhausted:
        # Defensive guard: a READY task should never reach this state, but
        # attempt_count must never exceed max_attempts under any path.
        return TransitionResult(task, False, "attempts already exhausted")

    new_task = replace(
        task,
        state=TaskState.RUNNING,
        attempt_count=task.attempt_count + 1,
        lease_owner=worker_id,
        lease_expires_at=now + lease_duration,
    )
    return TransitionResult(new_task, True)


def _lease_is_valid(task: TaskInstance, *, worker_id: str, now: datetime) -> bool:
    """True only if ``worker_id`` currently holds a live lease on ``task``."""
    return (
        task.state == TaskState.RUNNING
        and task.lease_owner == worker_id
        and task.lease_expires_at is not None
        and now <= task.lease_expires_at
    )


def report_success(task: TaskInstance, *, worker_id: str, now: datetime) -> TransitionResult:
    """RUNNING -> SUCCEEDED on a valid success report from the lease holder.

    Reports from anyone other than the current lease holder (wrong worker,
    lease expired, task already resolved by someone else) are stale/zombie
    reports and are safely ignored: no exception, no state change (see
    module docstring for why this differs from claim()'s handling of an
    invalid source state).
    """
    _require_utc(now, param_name="now")
    if not _lease_is_valid(task, worker_id=worker_id, now=now):
        return TransitionResult(
            task, False, "stale or non-owning worker report; ignored"
        )
    new_task = replace(
        task, state=TaskState.SUCCEEDED, lease_owner=None, lease_expires_at=None
    )
    return TransitionResult(new_task, True)


def report_failure(task: TaskInstance, *, worker_id: str, now: datetime) -> TransitionResult:
    """RUNNING -> READY (retry) or RUNNING -> FAILED_TERMINAL (exhausted).

    ``attempt_count`` is never touched here -- it was already incremented
    once, at claim time. Retry eligibility is decided by
    ``_resolve_after_failure`` (shared with ``expire_lease``).

    As with ``report_success``, reports from a worker that no longer owns
    the lease are safely ignored rather than raising (expected race; see
    module docstring). This must never be weakened to accept a report from
    a non-owning worker, even for the terminal-failure path.
    """
    _require_utc(now, param_name="now")
    if not _lease_is_valid(task, worker_id=worker_id, now=now):
        return TransitionResult(
            task, False, "stale or non-owning worker report; ignored"
        )
    next_state = _resolve_after_failure(task)
    new_task = replace(
        task, state=next_state, lease_owner=None, lease_expires_at=None
    )
    return TransitionResult(new_task, True)


def expire_lease(task: TaskInstance, *, now: datetime) -> TransitionResult:
    """RUNNING -> READY (retry) or RUNNING -> FAILED_TERMINAL (exhausted),
    driven by lease expiry rather than a worker report.

    This is the reaper's entry point (the reaper loop itself lives
    elsewhere and is not implemented here). Unlike ``report_failure``, it
    takes no ``worker_id`` -- a dead/zombie worker cannot report by
    definition -- and instead requires the lease to actually have expired.
    It shares the exact same retry-vs-terminal decision as
    ``report_failure`` via ``_resolve_after_failure``, so the rule is
    defined in exactly one place.

    Raises ``TransitionError`` if the task is not RUNNING, has no active
    lease, or the lease has not yet expired -- these all indicate the
    reaper's own query/timing was wrong, not a routine race.
    """
    _require_utc(now, param_name="now")
    if task.state != TaskState.RUNNING:
        raise TransitionError(
            f"cannot expire lease: task is {task.state}, not RUNNING"
        )
    if task.lease_expires_at is None:
        raise TransitionError("cannot expire lease: task has no active lease")
    if now <= task.lease_expires_at:
        raise TransitionError("cannot expire lease: lease has not yet expired")

    next_state = _resolve_after_failure(task)
    new_task = replace(
        task, state=next_state, lease_owner=None, lease_expires_at=None
    )
    return TransitionResult(new_task, True)


def skip(task: TaskInstance) -> TransitionResult:
    """PENDING -> SKIPPED via downstream dependency-failure cascade.

    Idempotent: skipping an already-SKIPPED task is a no-op
    (``applied=False``), so a cascade that visits the same downstream task
    twice (e.g. two failed dependencies both trigger propagation) is safe
    to run repeatedly.

    Any other state is a hop violation -- SKIPPED can only ever be reached
    from PENDING, never from READY/RUNNING/SUCCEEDED/FAILED_TERMINAL -- and
    raises ``TransitionError`` (caller bug; see module docstring).
    """
    if task.state == TaskState.SKIPPED:
        return TransitionResult(task, False, "already skipped; cascade is idempotent")
    if task.state != TaskState.PENDING:
        raise TransitionError(
            f"cannot skip: task is {task.state}; only PENDING tasks may be "
            "skipped via the dependency-failure cascade"
        )
    return TransitionResult(replace(task, state=TaskState.SKIPPED), True)
