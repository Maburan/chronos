"""Phase 5: direct tests for the shared execute_and_report() glue, now
extracted out of TaskWorker so worker.fallback_poller.FallbackPoller can
reuse it without duplication. Real PostgreSQL (same ``db`` fixture as the
rest of the suite) for the claim/report-outcome tests; deterministic
fakes (no real DB, no real sleeps) for the heartbeat-ordering/bounded-stop
regression tests below, since those are about call ORDER and TIMING
boundaries, not persistence behavior."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from core.transitions import TaskState
from db import persistence
from worker.executor import TaskExecutorRegistry
from worker.execution import _HEARTBEAT_STOP_TIMEOUT_SECONDS, execute_and_report

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class _FakeRecord:
    id: int
    task_type: str


def _make_workflow(db):
    return persistence.create_workflow(db, name="wf")


def _claim(db, task_id, worker_id="w1"):
    from datetime import timedelta

    return persistence.claim_task_by_id(
        db, task_id=task_id, worker_id=worker_id, now=NOW, lease_duration=timedelta(minutes=5)
    )


def test_successful_execution_reports_success(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    claimed = _claim(db, task.id)

    registry = TaskExecutorRegistry({"noop": lambda record: None})
    execute_and_report(db, registry, claimed, worker_id="w1", clock=lambda: NOW)

    with db.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (task.id,)).fetchone()
    assert row["state"] == "SUCCEEDED"


def test_raising_executor_reports_failure(db):
    wf = _make_workflow(db)
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="boom", max_attempts=1
    )
    claimed = _claim(db, task.id)

    def _boom(record):
        raise RuntimeError("nope")

    registry = TaskExecutorRegistry({"boom": _boom})
    execute_and_report(db, registry, claimed, worker_id="w1", clock=lambda: NOW)

    with db.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (task.id,)).fetchone()
    assert row["state"] == "FAILED_TERMINAL"


def test_worker_id_is_used_for_the_report(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    claimed = _claim(db, task.id, worker_id="worker-xyz")

    registry = TaskExecutorRegistry({"noop": lambda record: None})
    # A report from the WRONG worker_id must be ignored (existing fencing,
    # exercised here through the shared glue).
    execute_and_report(db, registry, claimed, worker_id="someone-else", clock=lambda: NOW)

    with db.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (task.id,)).fetchone()
    assert row["state"] == "RUNNING"  # report was ignored -- still claimed by worker-xyz


# ---------------------------------------------------------------------------
# heartbeat ordering regression: stop() must happen before either report
# call, not after (the bug the audit found and this fix closes)
# ---------------------------------------------------------------------------


class _FakeHeartbeat:
    """Records start()/stop() calls into a shared, ordered event log so
    tests can assert exactly when they happened relative to execution and
    reporting -- without a real thread, real DB, or real timing."""

    def __init__(self, events: list[str], *, stop_succeeds: bool = True):
        self._events = events
        self._stop_succeeds = stop_succeeds
        self.stop_calls: list[float | None] = []

    def __call__(self, pool, *, task_id, worker_id, lease_duration, interval_seconds, clock):
        # Mimics LeaseHeartbeat's constructor signature exactly (positional
        # pool, keyword-only everything else) so it's a drop-in replacement.
        return self

    def start(self) -> None:
        self._events.append("heartbeat_start")

    def stop(self, *, timeout=None) -> bool:
        self.stop_calls.append(timeout)
        self._events.append("heartbeat_stop")
        return self._stop_succeeds


def test_heartbeat_is_stopped_before_report_success(monkeypatch):
    events: list[str] = []
    fake_heartbeat = _FakeHeartbeat(events)
    monkeypatch.setattr("worker.execution.LeaseHeartbeat", fake_heartbeat)
    monkeypatch.setattr(
        persistence,
        "report_task_success",
        lambda pool, *, task_id, worker_id, now: events.append("report_success"),
    )

    registry = TaskExecutorRegistry({"noop": lambda record: events.append("execute")})
    record = _FakeRecord(id=1, task_type="noop")

    execute_and_report(
        pool=None,
        executors=registry,
        record=record,
        worker_id="w1",
        clock=lambda: NOW,
        lease_duration=timedelta(minutes=5),
    )

    assert events == ["heartbeat_start", "execute", "heartbeat_stop", "report_success"]
    # And it must have been stopped with an explicit, bounded timeout --
    # never the unbounded (join-forever) default.
    assert fake_heartbeat.stop_calls == [_HEARTBEAT_STOP_TIMEOUT_SECONDS]


def test_heartbeat_is_stopped_before_report_failure(monkeypatch):
    events: list[str] = []
    fake_heartbeat = _FakeHeartbeat(events)
    monkeypatch.setattr("worker.execution.LeaseHeartbeat", fake_heartbeat)
    monkeypatch.setattr(
        persistence,
        "report_task_failure",
        lambda pool, *, task_id, worker_id, now: events.append("report_failure"),
    )

    def _boom(record):
        events.append("execute")
        raise RuntimeError("nope")

    registry = TaskExecutorRegistry({"boom": _boom})
    record = _FakeRecord(id=1, task_type="boom")

    execute_and_report(
        pool=None,
        executors=registry,
        record=record,
        worker_id="w1",
        clock=lambda: NOW,
        lease_duration=timedelta(minutes=5),
    )

    assert events == ["heartbeat_start", "execute", "heartbeat_stop", "report_failure"]
    assert fake_heartbeat.stop_calls == [_HEARTBEAT_STOP_TIMEOUT_SECONDS]


def test_no_heartbeat_when_lease_duration_not_given(monkeypatch):
    """Backward-compatible default: lease_duration=None must never
    construct or touch LeaseHeartbeat at all."""
    events: list[str] = []

    def _fail_if_constructed(*args, **kwargs):
        raise AssertionError("LeaseHeartbeat must not be constructed when lease_duration is None")

    monkeypatch.setattr("worker.execution.LeaseHeartbeat", _fail_if_constructed)
    monkeypatch.setattr(
        persistence,
        "report_task_success",
        lambda pool, *, task_id, worker_id, now: events.append("report_success"),
    )

    registry = TaskExecutorRegistry({"noop": lambda record: events.append("execute")})
    record = _FakeRecord(id=1, task_type="noop")

    execute_and_report(
        pool=None, executors=registry, record=record, worker_id="w1", clock=lambda: NOW
    )

    assert events == ["execute", "report_success"]


# ---------------------------------------------------------------------------
# bounded-stop regression: a heartbeat that never terminates promptly must
# not block execute_and_report indefinitely, and reporting must still occur
# ---------------------------------------------------------------------------


def test_stuck_heartbeat_does_not_block_reporting(monkeypatch, caplog):
    events: list[str] = []
    # stop_succeeds=False simulates LeaseHeartbeat.stop() returning False --
    # i.e. the background thread was still alive when the bounded wait
    # elapsed. No real sleeping happens here: the fake's stop() returns
    # immediately with a "not stopped" result, exactly as the real
    # LeaseHeartbeat.stop() would after its own internal (bounded) join
    # times out -- this test proves execute_and_report's OWN behavior in
    # that situation, not LeaseHeartbeat's internal timing.
    fake_heartbeat = _FakeHeartbeat(events, stop_succeeds=False)
    monkeypatch.setattr("worker.execution.LeaseHeartbeat", fake_heartbeat)
    monkeypatch.setattr(
        persistence,
        "report_task_success",
        lambda pool, *, task_id, worker_id, now: events.append("report_success"),
    )

    registry = TaskExecutorRegistry({"noop": lambda record: events.append("execute")})
    record = _FakeRecord(id=1, task_type="noop")

    with caplog.at_level("WARNING", logger="worker.execution"):
        start = time.monotonic()
        execute_and_report(
            pool=None,
            executors=registry,
            record=record,
            worker_id="w1",
            clock=lambda: NOW,
            lease_duration=timedelta(minutes=5),
        )
        elapsed = time.monotonic() - start

    assert elapsed < 1.0  # never blocks -- no real waiting happened at all here
    # Reporting still happens, in order, after the bounded stop attempt --
    # a stuck heartbeat is never silently turned into success or failure.
    assert events == ["heartbeat_start", "execute", "heartbeat_stop", "report_success"]
    assert fake_heartbeat.stop_calls == [_HEARTBEAT_STOP_TIMEOUT_SECONDS]
    assert any("did not stop" in record.message for record in caplog.records)


def test_stuck_heartbeat_does_not_block_failure_reporting(monkeypatch):
    events: list[str] = []
    fake_heartbeat = _FakeHeartbeat(events, stop_succeeds=False)
    monkeypatch.setattr("worker.execution.LeaseHeartbeat", fake_heartbeat)
    monkeypatch.setattr(
        persistence,
        "report_task_failure",
        lambda pool, *, task_id, worker_id, now: events.append("report_failure"),
    )

    def _boom(record):
        events.append("execute")
        raise RuntimeError("nope")

    registry = TaskExecutorRegistry({"boom": _boom})
    record = _FakeRecord(id=1, task_type="boom")

    start = time.monotonic()
    execute_and_report(
        pool=None,
        executors=registry,
        record=record,
        worker_id="w1",
        clock=lambda: NOW,
        lease_duration=timedelta(minutes=5),
    )
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    assert events == ["heartbeat_start", "execute", "heartbeat_stop", "report_failure"]
