"""The autoscaler control loop.

One pass (``run_once``):

    for each managed task type:
        backlog  = READY task count      (from PostgreSQL, authoritative)
        current  = running replica count (from the WorkerRuntime)
        desired  = policy.desired_replicas(backlog)   (clamped)
        reconcile(backlog, current, desired)

``reconcile`` is where anti-thrash lives, and it is deliberately
asymmetric (scale up fast, scale down slow):

  * desired == current -> no-op. The runtime is never called; nothing is
    logged (a quiet steady state must not spam the log every interval).

  * desired  >  current -> SCALE-UP STABILIZATION. A single spike
    observation is not enough: the desired-higher condition must hold for
    ``scale_up_stabilization`` consecutive passes before we scale. This
    still reacts within a couple of intervals, so a real spike is handled
    quickly, but a one-cycle blip is ignored.

  * desired  <  current -> SCALE-DOWN COOLDOWN. We record when the queue
    first went quiet and only scale down once it has stayed quiet for
    ``scale_down_cooldown_seconds``. A queue that briefly empties and
    refills never loses its workers. While the cooldown is counting down
    we log "scale-down cooldown active" exactly once per episode, not
    every cycle.

Each task type carries its OWN independent state (streak + quiet-since),
so echo scaling up never affects sleep's cooldown, etc.

Safety (unchanged from the horizontal-scaling design, restated because
autoscaling must not weaken it): this loop only ever asks the runtime to
change a replica *count*. It never touches task state, never claims/acks,
never manipulates a lease. Scaling a worker down is a plain Compose
stop of a container; a worker mid-task either finishes during its
graceful-shutdown grace period (worker/__main__.py handles SIGTERM by
letting the current run_once complete) or, if hard-killed, leaves its task
RUNNING for the Reaper to reclaim -- exactly the crash-recovery path that
already exists and is tested. No task is lost or run twice because of a
scale operation.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional

from autoscaler.config import AutoscalerConfig
from autoscaler.policy import ScalingPolicy
from autoscaler.runtime import WorkerRuntime

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
# Returns {task_type: backlog(READY count)}. Injected so the loop can be
# tested with no database, and so the DB-reading code lives in __main__.
BacklogProvider = Callable[[], Mapping[str, int]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _TypeState:
    """Per-task-type anti-thrash state. ``up_streak`` counts consecutive
    passes that wanted more replicas; ``quiet_since`` marks when the queue
    first wanted fewer. ``last_log`` dedupes the non-action log lines."""

    up_streak: int = 0
    quiet_since: Optional[datetime] = None
    last_log: Optional[str] = None


@dataclass(frozen=True)
class ReconcileOutcome:
    """What one task type's reconcile decided this pass -- returned so
    tests can assert behavior without scraping logs."""

    task_type: str
    backlog: int
    current: int
    desired: int
    action: str  # "noop" | "scaled_up" | "scaled_down" | "stabilizing" | "cooldown"


class Autoscaler:
    def __init__(
        self,
        *,
        backlog_provider: BacklogProvider,
        runtime: WorkerRuntime,
        policy: ScalingPolicy,
        config: AutoscalerConfig,
        clock: Clock = _utc_now,
    ) -> None:
        self._backlog_provider = backlog_provider
        self._runtime = runtime
        self._policy = policy
        self._config = config
        self._clock = clock
        self._state: dict[str, _TypeState] = {
            t: _TypeState() for t in config.task_types
        }

    # -- one pass ----------------------------------------------------------

    def run_once(self) -> list[ReconcileOutcome]:
        backlog_by_type = self._backlog_provider()
        outcomes: list[ReconcileOutcome] = []
        now = self._clock()
        for task_type in self._config.task_types:
            backlog = int(backlog_by_type.get(task_type, 0))
            current = self._runtime.current_replicas(task_type)
            desired = self._policy.desired_replicas(backlog)
            outcomes.append(
                self._reconcile(task_type, backlog, current, desired, now)
            )
        return outcomes

    def _reconcile(
        self, task_type: str, backlog: int, current: int, desired: int, now: datetime
    ) -> ReconcileOutcome:
        state = self._state.setdefault(task_type, _TypeState())
        service = f"{self._config.service_prefix}{task_type}"

        if desired == current:
            state.up_streak = 0
            state.quiet_since = None
            return ReconcileOutcome(task_type, backlog, current, desired, "noop")

        if desired > current:
            state.quiet_since = None
            state.up_streak += 1
            if state.up_streak >= self._config.scale_up_stabilization:
                logger.info(
                    "[autoscaler] %s backlog=%d current=%d desired=%d",
                    task_type, backlog, current, desired,
                )
                logger.info(
                    "[autoscaler] scaling %s %d -> %d", service, current, desired
                )
                self._runtime.scale(task_type, desired)
                state.up_streak = 0
                state.last_log = None
                return ReconcileOutcome(
                    task_type, backlog, current, desired, "scaled_up"
                )
            self._log_once(
                state,
                "[autoscaler] %s backlog=%d current=%d desired=%d "
                "(scale-up stabilizing %d/%d)"
                % (
                    task_type, backlog, current, desired,
                    state.up_streak, self._config.scale_up_stabilization,
                ),
            )
            return ReconcileOutcome(
                task_type, backlog, current, desired, "stabilizing"
            )

        # desired < current -> scale down, gated by cooldown.
        state.up_streak = 0
        if state.quiet_since is None:
            state.quiet_since = now
        elapsed = (now - state.quiet_since).total_seconds()
        if elapsed >= self._config.scale_down_cooldown_seconds:
            logger.info(
                "[autoscaler] %s backlog=%d current=%d desired=%d",
                task_type, backlog, current, desired,
            )
            logger.info(
                "[autoscaler] scaling %s %d -> %d (cooldown elapsed)",
                service, current, desired,
            )
            self._runtime.scale(task_type, desired)
            state.quiet_since = None
            state.last_log = None
            return ReconcileOutcome(
                task_type, backlog, current, desired, "scaled_down"
            )
        self._log_once(
            state,
            "[autoscaler] %s backlog=%d current=%d desired=%d "
            "scale-down cooldown active (%.0fs/%.0fs)"
            % (
                task_type, backlog, current, desired,
                elapsed, self._config.scale_down_cooldown_seconds,
            ),
        )
        return ReconcileOutcome(task_type, backlog, current, desired, "cooldown")

    def _log_once(self, state: _TypeState, message: str) -> None:
        """Emit ``message`` only if it differs from the last one emitted
        for this task type -- keeps a steady 'stabilizing' or 'cooldown'
        state from printing every single interval."""
        if state.last_log != message:
            logger.info(message)
            state.last_log = message

    # -- continuous operation ---------------------------------------------

    def run_forever(
        self, *, stop_event: threading.Event, on_pass: Optional[Callable] = None
    ) -> None:
        """Poll every ``interval_seconds`` until ``stop_event`` is set. A
        failure in a single pass (Docker hiccup, transient DB error) is
        logged and swallowed -- the control loop must never crash on one
        bad observation, and it never touches task state, so a skipped
        pass is harmless (the next pass reconciles from fresh truth)."""
        logger.info(
            "[autoscaler] starting (interval=%.1fs min=%d max=%d "
            "scale_up_stabilization=%d scale_down_cooldown=%.0fs task_types=%s)",
            self._config.interval_seconds,
            self._config.min_replicas,
            self._config.max_replicas,
            self._config.scale_up_stabilization,
            self._config.scale_down_cooldown_seconds,
            ",".join(self._config.task_types) or "(none)",
        )
        while not stop_event.is_set():
            try:
                outcomes = self.run_once()
                if on_pass is not None:
                    on_pass(outcomes)
            except Exception:  # noqa: BLE001 - one bad pass must not kill the loop
                logger.warning("[autoscaler] pass failed; will retry", exc_info=True)
            stop_event.wait(self._config.interval_seconds)
