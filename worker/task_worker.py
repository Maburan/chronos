"""The worker: RabbitMQ dispatch hint -> targeted PostgreSQL claim
-> ACK -> execute -> report.

    RabbitMQ
        |
        v
    TaskWorker (this module)
        |
        | claim_task_by_id           <-- targeted claim; see db/persistence.py
        v
    PostgreSQL
        |
        v
    TaskExecutorRegistry.execute      <-- worker/executor.py
        |
        v
    report_task_success / report_task_failure

PostgreSQL remains authoritative throughout. The message's ``task_id`` is
used only as the target of one targeted claim attempt
(``persistence.claim_task_by_id``, backed by ``queries.CLAIM_TASK_BY_ID``)
-- never ``persistence.claim_task()`` (the untargeted "oldest READY task"
claim, reserved for the fallback poller -- see worker/fallback_poller.py).
Once claimed, the returned ``TaskRecord`` -- not the
message -- is what the executor receives and what the worker reports
against. ``workflow_id``/``task_type`` from the message itself are never
used to decide authoritative state (see worker/message.py).

ACK timing: the RabbitMQ message is acknowledged immediately after the
claim attempt resolves, whether or not it succeeded -- never after
execution finishes (see ``_handle_message`` below). RabbitMQ's job ends at
"was this dispatch hint delivered to a consumer," not "did the task
finish." A worker crash after ACK but mid-execution leaves the task
RUNNING under its lease in PostgreSQL; recovering that is the reaper's
job (see reaper/reaper.py), not this module's.

Duplicate/stale dispatch: ``claim_task_by_id`` returning ``None`` covers
every "nothing to do" case uniformly -- task doesn't exist, isn't READY
anymore, was already claimed by another worker, is terminal, or this is a
redelivered/duplicate message. No deduplication table, no event-id
tracking: PostgreSQL's conditional claim is the only protection needed.

Optionally, an admission gate: ``redis_limiter.limiter.TaskTypeLimiter``
gates the claim attempt with a per-task-type admission check, BEFORE
``claim_task_by_id`` is ever called. ``hint.task_type`` (from the message)
is used only to select which Redis concurrency bucket to check -- never
to decide any PostgreSQL-side outcome, same "message is a hint, not
authoritative" rule as ``task_id``/``workflow_id`` above. Redis is never
authoritative: any Redis failure fails open (admission granted) --
see ``redis_limiter/limiter.py``. If admission is denied, the message is
still ACKed and the function returns without ever calling
``claim_task_by_id`` -- no claim attempt, no attempt_count increment, no
PostgreSQL state change of any kind. If admission is granted but the
subsequent claim returns ``None`` (lost a race, task no longer READY),
the just-acquired slot is released immediately rather than held for its
full TTL. If the claim succeeds, the slot is held through
``execute_and_report`` and released exactly once afterward, in a
``finally``, regardless of success or failure.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from psycopg_pool import ConnectionPool

from db import persistence
from db.models import TaskRecord
from mq.connection import ConsumedMessage, MessageSource
from redis_limiter.limiter import TaskTypeLimiter
from worker.executor import TaskExecutorRegistry
from worker.execution import Clock, _utc_now, execute_and_report
from worker.message import InvalidDispatchHintError, parse_dispatch_hint

logger = logging.getLogger(__name__)


class TaskWorker:
    """Ties a ``MessageSource`` (RabbitMQ consumer or fake), the targeted
    claim, a ``TaskExecutorRegistry``, and the existing
    report_task_success/report_task_failure persistence calls together.

    Depends only on ``mq.connection.MessageSource`` -- never imports pika
    -- so tests can inject a fake message source with no real broker (see
    tests/test_worker_task_worker.py), the same separation
    ``OutboxRelay`` uses for ``ConfirmedPublisher``.
    """

    def __init__(
        self,
        pool: ConnectionPool,
        message_source: MessageSource,
        executors: TaskExecutorRegistry,
        *,
        worker_id: str,
        lease_duration,
        clock: Clock = _utc_now,
        heartbeat_interval_seconds: Optional[float] = None,
        limiter: Optional[TaskTypeLimiter] = None,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id is required")
        self._pool = pool
        self._source = message_source
        self._executors = executors
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._clock = clock
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._limiter = limiter

    # -- one message -------------------------------------------------

    def run_once(self) -> bool:
        """Process exactly one message if one is available within the
        message source's own timeout. Returns ``True`` if a message was
        consumed (regardless of whether it was malformed, claimed, or a
        no-op) -- it is always ACKed or rejected before this returns.
        Returns ``False`` if no message arrived (a normal, expected
        outcome -- lets a polling loop check a stop condition)."""
        message = self._source.next_message()
        if message is None:
            return False

        self._handle_message(message)
        return True

    def _handle_message(self, message: ConsumedMessage) -> None:
        try:
            hint = parse_dispatch_hint(message.body)
        except InvalidDispatchHintError:
            logger.warning("worker: rejecting malformed dispatch hint", exc_info=True)
            message.reject(requeue=False)
            return

        # Optional admission gate, BEFORE the claim attempt.
        # hint.task_type is used only to pick a Redis bucket -- it never
        # decides anything PostgreSQL-side. Denial means no claim attempt
        # is ever made: no attempt_count increment, no state change.
        slot_acquired = False
        if self._limiter is not None:
            slot_acquired = self._limiter.acquire(
                task_type=hint.task_type, worker_id=self._worker_id, task_id=hint.task_id
            )
            if not slot_acquired:
                message.ack()
                logger.info(
                    "worker: admission denied for event_id=%s task_id=%s task_type=%s; "
                    "skipping claim attempt this cycle",
                    hint.event_id,
                    hint.task_id,
                    hint.task_type,
                )
                return

        claimed = self._claim(hint.task_id)

        # ACK immediately once the claim attempt has resolved -- claimed
        # or not -- and BEFORE any execution happens. See module
        # docstring for why.
        message.ack()

        if claimed is None:
            # Nothing to do: not READY / already claimed / terminal /
            # stale or duplicate dispatch. Never falls back to the
            # untargeted claim_task(). A slot granted above but never
            # used for an actual claim must be released immediately,
            # not held for its full TTL.
            if self._limiter is not None and slot_acquired:
                self._limiter.release(
                    task_type=hint.task_type, worker_id=self._worker_id, task_id=hint.task_id
                )
            logger.info(
                "worker: event_id=%s task_id=%s not claimable "
                "(stale/duplicate/already resolved)",
                hint.event_id,
                hint.task_id,
            )
            return

        try:
            self._execute_and_report(claimed)
        finally:
            if self._limiter is not None and slot_acquired:
                self._limiter.release(
                    task_type=hint.task_type, worker_id=self._worker_id, task_id=hint.task_id
                )

    def _claim(self, task_id: int) -> Optional[TaskRecord]:
        return persistence.claim_task_by_id(
            self._pool,
            task_id=task_id,
            worker_id=self._worker_id,
            now=self._clock(),
            lease_duration=self._lease_duration,
        )

    def _execute_and_report(self, record: TaskRecord) -> None:
        # Shared with worker/fallback_poller.py's FallbackPoller -- see
        # worker/execution.py's module docstring for why this is extracted
        # rather than duplicated. lease_duration enables background
        # heartbeat renewal for the duration of execution.
        execute_and_report(
            self._pool,
            self._executors,
            record,
            worker_id=self._worker_id,
            clock=self._clock,
            lease_duration=self._lease_duration,
            heartbeat_interval_seconds=self._heartbeat_interval_seconds,
        )

    # -- continuous operation -----------------------------------------

    def run_forever(
        self,
        *,
        stop_check=lambda: False,
        idle_sleep_seconds: float = 0.1,
        on_iteration=None,
    ) -> None:
        """Consume until ``stop_check()`` returns True. No distributed
        coordination, no leader election -- multiple worker processes may
        run this loop concurrently and safely (PostgreSQL's conditional
        claim is what makes that safe, not anything in this loop).

        ``on_iteration``, if given, is called once per iteration with the
        ``bool`` ``run_once()`` returned (whether a message was consumed),
        purely for lightweight operational logging in the entrypoint
        (``worker/__main__.py``) -- no metrics backend, no new
        infrastructure. Defaults to ``None`` so every existing caller's
        behavior is unchanged.
        """
        while not stop_check():
            got_message = self.run_once()
            if on_iteration is not None:
                on_iteration(got_message)
            if not got_message:
                time.sleep(idle_sleep_seconds)
