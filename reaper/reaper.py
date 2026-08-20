"""The lease reaper: the only process that discovers and reclaims
RUNNING tasks whose lease has expired -- crash recovery for a worker that
claimed (and, for the RabbitMQ path, ACKed) a task and then died before
reporting success or failure.

    PostgreSQL
        |
        | RUNNING tasks with lease_expires_at < now()
        v
    Reaper (this module)
        |
        | persistence.expire_lease(task_id)   <-- existing, unmodified
        v                                         persistence-layer code
    PostgreSQL: READY (retry) or FAILED_TERMINAL

Nothing here duplicates state-machine logic. ``core.transitions
.expire_lease`` (frozen, unmodified) and ``db.persistence.expire_lease``
(itself unmodified since it was first written) already fully own the
retry-vs-terminal decision, attempt counting, the transactional outbox
event on retry, and the skip cascade on terminal failure -- this module
is purely: discover candidate task_ids, then call the existing wrapper
once per candidate.

Reaper-vs-worker races (the one genuinely new piece of logic this module
adds): discovery is a plain, unlocked read (see
``queries.SELECT_EXPIRED_LEASES`` / ``persistence
.list_expired_lease_task_ids``) -- by the time this loop gets around to
calling ``persistence.expire_lease`` on a given candidate, a worker may
have already completed that same task (``report_task_success``/
``report_task_failure``) or another reaper pass may have already reclaimed
it. ``core.transitions.expire_lease`` reacts to that by *raising*
``TransitionError`` (not returning ``applied=False`` -- see its
docstring), because the frozen module has no way to distinguish "a caller
bug" from "an expected discovery/lock race" -- that distinction is exactly
what this loop exists to make. A ``TransitionError`` here is therefore
caught and treated as an expected, ignorable lost race, never allowed to
crash the loop or abort the rest of the batch.

No distributed coordination, no leader election: multiple reaper
instances may run this loop concurrently and safely -- the same row-lock
+ re-validation inside ``expire_lease`` that fences a lost race against a
worker fences an equally lost race against another reaper instance,
identically.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from psycopg_pool import ConnectionPool

from core.transitions import TransitionError
from db import persistence
from reaper.config import ReaperConfig

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ReaperIterationResult:
    """What one ``run_once`` pass did -- for both ``run_forever``'s
    backoff decision and tests. ``lost_races`` is never treated as an
    error: it's the normal, expected outcome of a worker or another
    reaper instance winning a race this loop's own discovery scan simply
    couldn't have known about ahead of time."""

    candidates: int
    reclaimed: int
    lost_races: int
    errors: int


class Reaper:
    """Discovers RUNNING tasks with an expired lease and reclaims each via
    the existing ``persistence.expire_lease``. Never imports pika, never
    touches RabbitMQ, never writes SQL of its own beyond the one discovery
    query -- everything that actually decides task state remains entirely
    inside ``core/transitions.py`` and ``db/persistence.py``, unmodified.
    """

    def __init__(
        self,
        pool: ConnectionPool,
        *,
        config: Optional[ReaperConfig] = None,
        clock: Clock = _utc_now,
    ) -> None:
        self._pool = pool
        self._config = config or ReaperConfig()
        self._clock = clock

    # -- one discovery pass -----------------------------------------------

    def run_once(self) -> ReaperIterationResult:
        """Fetch one bounded batch of expired-lease candidates and attempt
        to reclaim each in turn. A single ``now`` is used for both the
        discovery scan and every per-candidate ``expire_lease`` call in
        this pass: since discovery already filtered for
        ``lease_expires_at < now``, that same ``now`` is guaranteed to
        satisfy ``core.transitions.expire_lease``'s own expiry check for
        any candidate that hasn't changed state in the meantime.
        """
        now = self._clock()
        candidate_ids = persistence.list_expired_lease_task_ids(
            self._pool, now=now, batch_size=self._config.batch_size
        )

        reclaimed = 0
        lost_races = 0
        errors = 0

        for task_id in candidate_ids:
            try:
                persistence.expire_lease(self._pool, task_id=task_id, now=now)
            except TransitionError:
                # Expected: the task was RUNNING with an expired lease at
                # discovery time, but a worker's report (or another
                # reaper's pass) got the row lock first and changed its
                # state before this call's own SELECT ... FOR UPDATE ran.
                # Not a bug -- see module docstring.
                logger.info(
                    "reaper: lost race reclaiming task_id=%s (already resolved elsewhere)",
                    task_id,
                )
                lost_races += 1
                continue
            except Exception:  # noqa: BLE001 - e.g. a transient DB error on this one candidate
                logger.warning(
                    "reaper: failed to reclaim task_id=%s", task_id, exc_info=True
                )
                errors += 1
                continue

            reclaimed += 1

        return ReaperIterationResult(
            candidates=len(candidate_ids),
            reclaimed=reclaimed,
            lost_races=lost_races,
            errors=errors,
        )

    # -- continuous operation ----------------------------------------------

    def run_forever(self, *, stop_check=lambda: False) -> None:
        """Poll indefinitely until ``stop_check()`` returns True.

        Simple deterministic exponential backoff, doubling from
        ``initial_backoff_seconds`` up to ``max_backoff_seconds`` on any
        pass that reported an unexpected error, reset to the initial
        value the moment a pass reports zero errors. Lost races never
        trigger backoff -- they're a normal outcome, not a failure mode
        (mirrors ``relay/outbox_relay.py``'s ``run_forever`` exactly).
        """
        backoff = self._config.initial_backoff_seconds

        while not stop_check():
            try:
                result = self.run_once()
            except Exception:  # noqa: BLE001 - e.g. the DB itself is unreachable
                logger.warning("reaper: discovery pass failed", exc_info=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, self._config.max_backoff_seconds)
                continue

            if result.errors > 0:
                time.sleep(backoff)
                backoff = min(backoff * 2, self._config.max_backoff_seconds)
            else:
                backoff = self._config.initial_backoff_seconds
                time.sleep(self._config.poll_interval_seconds)
