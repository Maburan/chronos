"""Autoscaling integration: the real control loop against a REAL
PostgreSQL backlog signal, with an in-memory runtime standing in for
Docker (the real Docker scaling path is exercised separately in the manual
E2E documented in README/HANDOVER -- an in-process test cannot create real
containers). This proves the end-to-end wiring backlog -> policy ->
reconcile -> runtime.scale using the actual get_backlog_counts query, and
that scale-up and (post-cooldown) scale-down both fire from live DB state.

This is autoscaling test 9 at the integration level too: draining the
queue and letting the cooldown elapse scales DOWN safely toward MIN
without ever touching task state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from autoscaler.autoscaler import Autoscaler
from autoscaler.config import AutoscalerConfig
from db import persistence

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)


class FakeRuntime:
    def __init__(self, initial):
        self.replicas = dict(initial)
        self.scale_calls = []

    def current_replicas(self, task_type):
        return self.replicas.get(task_type, 0)

    def scale(self, task_type, replicas):
        self.scale_calls.append((task_type, replicas))
        self.replicas[task_type] = replicas


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


def _autoscaler(db, runtime, clock):
    config = AutoscalerConfig(
        enabled=True,
        min_replicas=1,
        max_replicas=5,
        scale_up_stabilization=2,
        scale_down_cooldown_seconds=60.0,
        task_types=("echo",),
    )

    def backlog_provider():
        counts = persistence.get_backlog_counts(db)
        return {t: c["backlog"] for t, c in counts.items()}

    return Autoscaler(
        backlog_provider=backlog_provider,
        runtime=runtime,
        policy=config.policy(),
        config=config,
        clock=clock,
    )


def test_scale_up_then_drain_then_scale_down_from_real_db(db):
    wf = persistence.create_workflow(db, name="autoscale-wf", user_id=db.test_user_id)

    # 8 READY echo tasks -> policy desired = 3
    echo_ids = [
        persistence.create_task(db, workflow_id=wf["id"], name=f"e{i}", task_type="echo").id
        for i in range(8)
    ]

    runtime = FakeRuntime({"echo": 1})
    clock = Clock(NOW)
    auto = _autoscaler(db, runtime, clock)

    # -- scale UP (needs 2 consecutive observations) --------------------
    o1 = auto.run_once()
    assert o1[0].backlog == 8 and o1[0].desired == 3 and o1[0].action == "stabilizing"
    assert runtime.scale_calls == []

    o2 = auto.run_once()
    assert o2[0].action == "scaled_up"
    assert runtime.current_replicas("echo") == 3
    assert runtime.scale_calls == [("echo", 3)]

    # -- drain the queue for real: claim + report success on every task --
    for tid in echo_ids:
        persistence.claim_task_by_id(db, task_id=tid, worker_id="w1", now=NOW, lease_duration=LEASE)
        persistence.report_task_success(db, task_id=tid, worker_id="w1", now=NOW)

    # backlog is now 0 -> desired 1, but cooldown must gate the scale-down
    o3 = auto.run_once()
    assert o3[0].backlog == 0 and o3[0].desired == 1 and o3[0].action == "cooldown"
    assert runtime.current_replicas("echo") == 3  # unchanged during cooldown

    # -- scale DOWN only after cooldown elapses -------------------------
    clock.advance(61)
    o4 = auto.run_once()
    assert o4[0].action == "scaled_down"
    assert runtime.current_replicas("echo") == 1
    assert runtime.scale_calls == [("echo", 3), ("echo", 1)]

    # task state untouched by any of this: all 8 still SUCCEEDED
    counts = persistence.get_backlog_counts(db)
    assert counts.get("echo", {"backlog": 0}).get("backlog", 0) == 0
