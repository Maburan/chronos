"""Autoscaling tests 3-6 + 9 (unit level): the reconcile state machine --
scale-up stabilization, scale-down cooldown, no-op on steady state,
independent per-task-type state, and safe (never below min) scale-down.
Uses an in-memory FakeRuntime, a controllable clock, and a settable
backlog map -- no Docker, no DB."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from autoscaler.autoscaler import Autoscaler
from autoscaler.config import AutoscalerConfig
from autoscaler.policy import ScalingPolicy


class FakeRuntime:
    """In-memory WorkerRuntime: a replica count per task type, plus a log
    of every scale() call for assertions."""

    def __init__(self, initial: dict[str, int]):
        self.replicas = dict(initial)
        self.scale_calls: list[tuple[str, int]] = []

    def current_replicas(self, task_type: str) -> int:
        return self.replicas.get(task_type, 0)

    def scale(self, task_type: str, replicas: int) -> None:
        self.scale_calls.append((task_type, replicas))
        self.replicas[task_type] = replicas


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _build(*, backlog, runtime, clock, task_types=("echo",), stabilization=2, cooldown=60.0):
    config = AutoscalerConfig(
        enabled=True,
        interval_seconds=10.0,
        min_replicas=1,
        max_replicas=5,
        scale_up_stabilization=stabilization,
        scale_down_cooldown_seconds=cooldown,
        task_types=tuple(task_types),
    )
    return Autoscaler(
        backlog_provider=lambda: backlog,
        runtime=runtime,
        policy=config.policy(),
        config=config,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# no-op when desired == current
# ---------------------------------------------------------------------------


def test_no_op_when_desired_equals_current():
    backlog = {"echo": 1}  # desired = 1
    runtime = FakeRuntime({"echo": 1})
    auto = _build(backlog=backlog, runtime=runtime, clock=Clock(START))

    outcomes = auto.run_once()

    assert outcomes[0].action == "noop"
    assert runtime.scale_calls == []


# ---------------------------------------------------------------------------
# scale-up stabilization
# ---------------------------------------------------------------------------


def test_scale_up_requires_consecutive_observations():
    backlog = {"echo": 8}  # desired = 3
    runtime = FakeRuntime({"echo": 1})
    auto = _build(backlog=backlog, runtime=runtime, clock=Clock(START), stabilization=2)

    first = auto.run_once()
    assert first[0].action == "stabilizing"
    assert runtime.scale_calls == []  # not yet

    second = auto.run_once()
    assert second[0].action == "scaled_up"
    assert runtime.scale_calls == [("echo", 3)]
    assert runtime.current_replicas("echo") == 3


def test_scale_up_streak_resets_if_spike_disappears():
    backlog = {"echo": 8}  # desired 3
    runtime = FakeRuntime({"echo": 1})
    auto = _build(backlog=backlog, runtime=runtime, clock=Clock(START), stabilization=3)

    auto.run_once()  # streak 1
    # spike vanishes: backlog drops so desired == current (1) -> resets streak
    backlog["echo"] = 0
    reset = auto.run_once()
    assert reset[0].action == "noop"
    # spike returns; must build the full streak again, not continue from 1
    backlog["echo"] = 8
    auto.run_once()  # streak 1
    auto.run_once()  # streak 2
    assert runtime.scale_calls == []
    auto.run_once()  # streak 3 -> scale
    assert runtime.scale_calls == [("echo", 3)]


# ---------------------------------------------------------------------------
# scale-down cooldown
# ---------------------------------------------------------------------------


def test_scale_down_waits_for_cooldown():
    backlog = {"echo": 0}  # desired = 1
    runtime = FakeRuntime({"echo": 3})
    clock = Clock(START)
    auto = _build(backlog=backlog, runtime=runtime, clock=clock, cooldown=60.0)

    # first quiet observation: cooldown starts, no scale yet
    o1 = auto.run_once()
    assert o1[0].action == "cooldown"
    assert runtime.scale_calls == []

    # still within cooldown
    clock.advance(30)
    o2 = auto.run_once()
    assert o2[0].action == "cooldown"
    assert runtime.scale_calls == []

    # cooldown elapsed -> scale down to desired
    clock.advance(31)  # total 61s > 60
    o3 = auto.run_once()
    assert o3[0].action == "scaled_down"
    assert runtime.scale_calls == [("echo", 1)]
    assert runtime.current_replicas("echo") == 1


def test_scale_down_cooldown_resets_if_backlog_returns():
    backlog = {"echo": 0}  # desired 1, current 3
    runtime = FakeRuntime({"echo": 3})
    clock = Clock(START)
    auto = _build(backlog=backlog, runtime=runtime, clock=clock, cooldown=60.0)

    auto.run_once()  # quiet_since = START
    clock.advance(40)
    # backlog spikes back up before cooldown elapses -> desired>=current, reset
    backlog["echo"] = 8  # desired 3 == current 3 -> noop, quiet_since cleared
    auto.run_once()
    # quiet again; cooldown must restart from now, not the original START
    backlog["echo"] = 0
    clock.advance(30)
    auto.run_once()  # quiet_since reset here
    clock.advance(30)  # only 30s into the new cooldown
    o = auto.run_once()
    assert o[0].action == "cooldown"
    assert runtime.scale_calls == []


def test_scale_down_never_below_min():
    backlog = {"echo": 0}
    runtime = FakeRuntime({"echo": 2})
    clock = Clock(START)
    auto = _build(backlog=backlog, runtime=runtime, clock=clock, cooldown=10.0)
    auto.run_once()
    clock.advance(11)
    auto.run_once()
    # desired floored at min_replicas=1, never 0
    assert runtime.scale_calls == [("echo", 1)]


# ---------------------------------------------------------------------------
# independent scaling per task type
# ---------------------------------------------------------------------------


def test_each_task_type_scales_independently():
    backlog = {"echo": 21, "sleep": 0}  # echo wants 5, sleep wants 1
    runtime = FakeRuntime({"echo": 1, "sleep": 3})
    clock = Clock(START)
    auto = _build(
        backlog=backlog, runtime=runtime, clock=clock,
        task_types=("echo", "sleep"), stabilization=1, cooldown=60.0,
    )

    outcomes = {o.task_type: o for o in auto.run_once()}
    # echo scales up immediately (stabilization=1); sleep enters cooldown
    assert outcomes["echo"].action == "scaled_up"
    assert runtime.current_replicas("echo") == 5
    assert outcomes["sleep"].action == "cooldown"
    assert runtime.current_replicas("sleep") == 3  # unchanged, still cooling down

    # sleep's cooldown is on its OWN timeline; echo's scale didn't affect it
    clock.advance(61)
    outcomes = {o.task_type: o for o in auto.run_once()}
    assert outcomes["echo"].action == "noop"  # already at 5
    assert outcomes["sleep"].action == "scaled_down"
    assert runtime.current_replicas("sleep") == 1


def test_missing_task_type_treated_as_zero_backlog():
    # nothing in the backlog map for echo -> backlog 0 -> desired 1
    runtime = FakeRuntime({"echo": 1})
    auto = _build(backlog={}, runtime=runtime, clock=Clock(START))
    outcomes = auto.run_once()
    assert outcomes[0].backlog == 0
    assert outcomes[0].action == "noop"
