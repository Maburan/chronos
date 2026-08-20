"""The heartbeat-phase background lease renewer.

``TaskExecutorFn`` (see worker/executor.py) is a single synchronous,
blocking call -- there is no cooperative yield point during execution for
a worker loop to "check in" from. A background thread is therefore the
only way to renew a lease *while* execution is in progress; this module
is the smallest correct primitive for that: start a thread on claim,
periodically call the existing ``persistence.renew_lease``, stop the
thread once execution finishes (success or failure) -- nothing more.

Deliberately NOT introduced here: any cancellation/cooperative-
cancellation mechanism. If ``renew_lease`` reports the lease has been
lost (fenced out -- see ``LeaseHeartbeat._run``), this module stops
renewing and logs it, and nothing else. The synchronous executor call
already in progress keeps running to completion; the existing at-least-
once/possible-duplicate-execution tradeoff (handoff sections 8/15)
already accounts for exactly this outcome. This is an explicit, approved
scope boundary for this phase, not an oversight.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Optional

from psycopg_pool import ConnectionPool

from db import persistence
from worker.clock import Clock, _utc_now

logger = logging.getLogger(__name__)


class LeaseHeartbeat:
    """Periodically renews the lease on one claimed task, in a background
    daemon thread, until stopped.

    Usable directly (``start()``/``stop()``) or as a context manager. One
    instance is scoped to exactly one claimed task's execution -- never
    reused across tasks.
    """

    def __init__(
        self,
        pool: ConnectionPool,
        *,
        task_id: int,
        worker_id: str,
        lease_duration: timedelta,
        interval_seconds: Optional[float] = None,
        clock: Clock = _utc_now,
    ) -> None:
        self._pool = pool
        self._task_id = task_id
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        # Default interval: roughly lease_duration / 3, so a single missed
        # or slow heartbeat doesn't immediately let the lease expire.
        self._interval_seconds = (
            interval_seconds
            if interval_seconds is not None
            else max(lease_duration.total_seconds() / 3, 0.01)
        )
        self._clock = clock
        self._stop_event = threading.Event()
        self._lost_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def lost(self) -> bool:
        """True once a renewal attempt has come back fenced out (task no
        longer RUNNING under this worker's ownership). Never reset -- once
        lost, always lost, for the lifetime of this instance."""
        return self._lost_event.is_set()

    def start(self) -> None:
        if self._thread is not None:
            return  # already started -- idempotent, not an error
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"lease-heartbeat-{self._task_id}", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: Optional[float] = None) -> bool:
        """Signal the background thread to stop and wait up to
        ``timeout`` seconds for it -- prompt, not tied to the polling
        interval: the thread wakes immediately on ``_stop_event``
        regardless of how long is left until its next scheduled renewal.

        Returns ``True`` once the thread has actually exited, ``False``
        if it was still alive when the wait bound elapsed (only possible
        with an explicit ``timeout`` and a genuinely stuck ``renew_lease``
        call in flight). ``timeout=None`` (the default) waits
        indefinitely, preserving this method's original behavior for any
        caller that doesn't pass one -- latency-sensitive callers (see
        ``worker/execution.py``) must pass an explicit bound rather than
        relying on this default.
        """
        self._stop_event.set()
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout)
        stopped = not self._thread.is_alive()
        if stopped:
            self._thread = None
        return stopped

    def __enter__(self) -> "LeaseHeartbeat":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    def _run(self) -> None:
        # Wait first: a lease starts fresh at claim time, no immediate
        # renewal is needed before the first interval has elapsed.
        while not self._stop_event.wait(self._interval_seconds):
            if self._lost_event.is_set():
                return

            try:
                result = persistence.renew_lease(
                    self._pool,
                    task_id=self._task_id,
                    worker_id=self._worker_id,
                    now=self._clock(),
                    lease_duration=self._lease_duration,
                )
            except Exception:  # noqa: BLE001 - a DB/connection failure is transient, not "lost"
                # Per the approved decision: a raised exception here means
                # the renewal attempt itself failed to run (transient --
                # connection blip, DB unavailable, etc.), NOT that the
                # lease has been fenced away. Log and retry on the next
                # interval; never conclude the lease is lost merely
                # because this one call failed.
                logger.warning(
                    "heartbeat: transient failure renewing lease for task_id=%s; "
                    "will retry next interval",
                    self._task_id,
                    exc_info=True,
                )
                continue

            if result is None:
                # Fenced out: task no longer RUNNING under this worker's
                # ownership (reaper reclaimed it, or the task already
                # completed). Stop renewing and log clearly. Deliberately
                # does NOT attempt to cancel the in-progress synchronous
                # executor call -- see module docstring.
                logger.warning(
                    "heartbeat: lease lost for task_id=%s worker_id=%s "
                    "(reclaimed or already completed); stopping heartbeat",
                    self._task_id,
                    self._worker_id,
                )
                self._lost_event.set()
                return
