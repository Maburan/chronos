"""The fallback poller: obtains work by polling PostgreSQL directly, with
no RabbitMQ dispatch hint at all.

    PostgreSQL claim_task()      <-- untargeted, "oldest READY task";
        |                            see db/persistence.py. Never
        |                            claim_task_by_id() -- there is no
        |                            task_id to target here.
        v
    TaskExecutorRegistry.execute  <-- worker/executor.py (same registry
        |                             type TaskWorker uses)
        v
    report_task_success / report_task_failure   <-- via
                                                     worker.execution.execute_and_report,
                                                     the exact same
                                                     function TaskWorker uses.

This exists for exactly one documented scenario (handoff section 18):
"Fallback polling handles: broker unavailable entirely." It is not a
replacement for the RabbitMQ-driven ``worker.task_worker.TaskWorker`` --
the two are meant to run concurrently, safely, side by side (see
``tests/test_fallback_poller.py``'s real-concurrency coverage). Nothing
here is a second claiming mechanism: it is the same, pre-existing,
untargeted ``claim_task()`` every other "no specific task in mind" caller
would use, and its safety under concurrent claimants (including a
simultaneously-running ``TaskWorker``) comes entirely from
``queries.CLAIM_READY_TASK``'s existing ``FOR UPDATE SKIP LOCKED``
guarantee -- nothing new is introduced here to make that safe.

Execution/reporting is intentionally NOT duplicated from
``worker.task_worker.TaskWorker`` -- both call
``worker.execution.execute_and_report`` directly. The only difference
between the two classes is how a ``TaskRecord`` is obtained.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Optional

from psycopg_pool import ConnectionPool

from db import persistence
from worker.executor import TaskExecutorRegistry
from worker.execution import Clock, _utc_now, execute_and_report


class FallbackPoller:
    """Polls PostgreSQL directly via the untargeted ``claim_task()``.
    Depends on nothing RabbitMQ-related -- no ``mq`` import anywhere in
    this module -- since the entire point is to keep working when the
    broker/relay path is unavailable."""

    def __init__(
        self,
        pool: ConnectionPool,
        executors: TaskExecutorRegistry,
        *,
        worker_id: str,
        lease_duration: timedelta,
        clock: Clock = _utc_now,
        heartbeat_interval_seconds: Optional[float] = None,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id is required")
        self._pool = pool
        self._executors = executors
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._clock = clock
        self._heartbeat_interval_seconds = heartbeat_interval_seconds

    def run_once(self) -> bool:
        """Attempt one untargeted claim. Returns ``True`` if a task was
        claimed and executed/reported, ``False`` if nothing was
        claimable -- a normal, expected outcome, never an error."""
        claimed = persistence.claim_task(
            self._pool,
            worker_id=self._worker_id,
            now=self._clock(),
            lease_duration=self._lease_duration,
        )
        if claimed is None:
            return False

        execute_and_report(
            self._pool,
            self._executors,
            claimed,
            worker_id=self._worker_id,
            clock=self._clock,
            lease_duration=self._lease_duration,
            heartbeat_interval_seconds=self._heartbeat_interval_seconds,
        )
        return True

    def run_forever(self, *, stop_check=lambda: False, idle_sleep_seconds: float = 0.1) -> None:
        """Poll until ``stop_check()`` returns True. No distributed
        coordination, no leader election -- may run concurrently with any
        number of other ``FallbackPoller`` instances and/or
        ``TaskWorker`` instances; PostgreSQL's conditional claim is what
        makes that safe, not anything in this loop."""
        while not stop_check():
            got_task = self.run_once()
            if not got_task:
                time.sleep(idle_sleep_seconds)
