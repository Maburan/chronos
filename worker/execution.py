"""The one piece of execution/reporting glue shared by every way Chronos
obtains a claimed task -- the RabbitMQ-driven ``worker.task_worker
.TaskWorker`` and the ``worker.fallback_poller.FallbackPoller``.

Extracted here (rather than left as a private method on ``TaskWorker``)
specifically so the fallback poller can reuse it without duplicating
claim -> execute -> report logic. The two callers differ *only* in how a
``TaskRecord`` is obtained (a RabbitMQ dispatch hint + ``claim_task_by_id``
for ``TaskWorker``; a direct, untargeted ``claim_task()`` call for
``FallbackPoller``) -- once a record is claimed, what happens to it is
identical, and now lives in exactly one place.

Heartbeat phase addition: when ``lease_duration`` is supplied, the
executor call is wrapped in a ``worker.heartbeat.LeaseHeartbeat`` --
started right before ``executors.execute(record)``, and stopped
(bounded -- see ``_HEARTBEAT_STOP_TIMEOUT_SECONDS``) immediately after
execution finishes (success or exception), strictly BEFORE either report
call runs. This ordering is deliberate: a report call clears the whole
lease atomically, and there's no reason for the heartbeat thread to still
be issuing renewal calls while that happens. Passing ``lease_duration=None``
(the default) disables heartbeating entirely, preserving this function's
pre-heartbeat behavior exactly for any caller that doesn't opt in.
"""

from __future__ import annotations

import logging
from typing import Optional

from datetime import timedelta

from psycopg_pool import ConnectionPool

from db import persistence
from db.models import TaskRecord
from worker.clock import Clock, _utc_now
from worker.executor import TaskExecutorRegistry
from worker.heartbeat import LeaseHeartbeat

logger = logging.getLogger(__name__)

# Bounded wait for the heartbeat thread to stop before reporting. Chosen
# to comfortably exceed one ordinary renew_lease DB round-trip
# (milliseconds under normal conditions) while staying short enough that
# a genuinely stuck DB connection can't block the report indefinitely.
# This is a worker-loop liveness bound, not a correctness boundary -- the
# heartbeat thread is a daemon thread regardless, so even if it never
# stops, the process itself is never blocked from exiting; only this one
# execute_and_report call's progress to its report step is bounded by it.
_HEARTBEAT_STOP_TIMEOUT_SECONDS = 5.0


def execute_and_report(
    pool: ConnectionPool,
    executors: TaskExecutorRegistry,
    record: TaskRecord,
    *,
    worker_id: str,
    clock: Clock,
    lease_duration: Optional[timedelta] = None,
    heartbeat_interval_seconds: Optional[float] = None,
) -> None:
    """Run the registered executor for ``record.task_type`` and report the
    outcome through the existing, unmodified persistence layer.

    Any exception raised by the executor (including
    ``worker.executor.UnregisteredTaskTypeError``) is treated uniformly as
    an execution failure -> ``report_task_failure``, which already owns
    every retry-vs-terminal, attempt-count, and skip-cascade decision (see
    ``core/transitions.py`` and ``db/persistence.py``) -- nothing here
    duplicates or second-guesses that logic.

    ``lease_duration``, if given, enables a background ``LeaseHeartbeat``
    for the duration of the (synchronous, blocking) executor call. If the
    heartbeat loses the lease (fenced out), it stops and logs on its own
    (see ``worker/heartbeat.py``) -- this function does not check
    ``heartbeat.lost`` or otherwise react to it: the executor call already
    in progress is never cancelled or aborted, by explicit design for this
    phase. Losing the heartbeat never changes whether the eventual report
    is success or failure -- that is decided entirely by whether
    ``executors.execute(record)`` raised, nothing else.
    """
    heartbeat: Optional[LeaseHeartbeat] = None
    if lease_duration is not None:
        heartbeat = LeaseHeartbeat(
            pool,
            task_id=record.id,
            worker_id=worker_id,
            lease_duration=lease_duration,
            interval_seconds=heartbeat_interval_seconds,
            clock=clock,
        )
        heartbeat.start()

    execution_failed = False
    try:
        executors.execute(record)
    except Exception:  # noqa: BLE001 - any execution failure, incl. UnregisteredTaskTypeError
        execution_failed = True
        logger.warning(
            "execute_and_report: task_id=%s execution failed", record.id, exc_info=True
        )
    finally:
        # Stopped here, BEFORE either report call below, and with a bound
        # so a stuck renew_lease call can never hang this function
        # indefinitely -- see _HEARTBEAT_STOP_TIMEOUT_SECONDS. A timeout
        # here is logged, not swallowed, and execution still proceeds to
        # report the task's real outcome (never treated as a success or
        # failure signal in its own right).
        if heartbeat is not None:
            stopped = heartbeat.stop(timeout=_HEARTBEAT_STOP_TIMEOUT_SECONDS)
            if not stopped:
                logger.warning(
                    "execute_and_report: heartbeat thread for task_id=%s did not stop "
                    "within %.1fs; proceeding to report the task's outcome anyway "
                    "(the thread is a daemon and cannot block process exit, but may "
                    "still be mid-renewal)",
                    record.id,
                    _HEARTBEAT_STOP_TIMEOUT_SECONDS,
                )

    if execution_failed:
        persistence.report_task_failure(pool, task_id=record.id, worker_id=worker_id, now=clock())
    else:
        persistence.report_task_success(pool, task_id=record.id, worker_id=worker_id, now=clock())
