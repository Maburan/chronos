"""Heartbeat-phase tests for worker.heartbeat.LeaseHeartbeat's lifecycle
(periodic renewal, prompt stop, fencing behavior, transient-failure
retry). Real PostgreSQL for the actual renewal calls -- the whole point
of these tests is to exercise real, timed background-thread behavior
against a real lease, not a faked persistence layer.

Every test uses a "synchronized fake clock": a callable anchored at the
fixed test epoch (``NOW``) that advances by real elapsed wall-clock time
(``time.monotonic()`` deltas). This is required because the background
thread's own pacing (``threading.Event.wait``) is genuinely real-time --
there's no way to fake that away for a real thread -- but every other
test in this codebase uses a fixed, far-in-the-past ``NOW`` for
determinism. Using the real wall clock (``datetime.now()``) here would
make every lease look already-expired relative to a lease computed from
``NOW``, since real wall-clock time is far ahead of the fixed test epoch.
The synchronized clock keeps both anchored together while still advancing
in real time, exactly like the real system would.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from db import persistence
from worker.heartbeat import LeaseHeartbeat

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
LEASE = timedelta(seconds=5)


def _synced_clock():
    """Returns a callable that reads like ``NOW`` but advances in step
    with real elapsed time from the moment this is called."""
    start_monotonic = time.monotonic()

    def _clock() -> datetime:
        return NOW + timedelta(seconds=time.monotonic() - start_monotonic)

    return _clock


def _make_workflow(db):
    return persistence.create_workflow(db, name="wf")


def _claim(db, *, worker_id="w1"):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    persistence.claim_task_by_id(db, task_id=task.id, worker_id=worker_id, now=NOW, lease_duration=LEASE)
    return task.id


def _lease_expiry(db, task_id):
    with db.connection() as conn:
        return conn.execute(
            "SELECT lease_expires_at FROM tasks WHERE id = %s", (task_id,)
        ).fetchone()["lease_expires_at"]


# ---------------------------------------------------------------------------
# periodic renewal / prompt stop
# ---------------------------------------------------------------------------


def test_heartbeat_periodically_renews_the_lease(db):
    task_id = _claim(db)
    initial_expiry = _lease_expiry(db, task_id)

    heartbeat = LeaseHeartbeat(
        db,
        task_id=task_id,
        worker_id="w1",
        lease_duration=LEASE,
        interval_seconds=0.05,
        clock=_synced_clock(),
    )
    heartbeat.start()
    try:
        time.sleep(0.25)  # several intervals' worth
    finally:
        heartbeat.stop(timeout=2.0)

    final_expiry = _lease_expiry(db, task_id)
    assert final_expiry > initial_expiry
    assert heartbeat.lost is False


def test_heartbeat_stop_is_prompt_not_tied_to_interval(db):
    task_id = _claim(db)
    heartbeat = LeaseHeartbeat(
        db,
        task_id=task_id,
        worker_id="w1",
        lease_duration=LEASE,
        interval_seconds=10.0,
        clock=_synced_clock(),
    )
    heartbeat.start()

    start = time.monotonic()
    heartbeat.stop(timeout=2.0)
    elapsed = time.monotonic() - start

    # Must not wait anywhere near the 10s interval to stop.
    assert elapsed < 1.0


def test_heartbeat_start_is_idempotent(db):
    task_id = _claim(db)
    heartbeat = LeaseHeartbeat(
        db,
        task_id=task_id,
        worker_id="w1",
        lease_duration=LEASE,
        interval_seconds=1.0,
        clock=_synced_clock(),
    )
    heartbeat.start()
    heartbeat.start()  # must not raise or spawn a second thread
    heartbeat.stop(timeout=2.0)


def test_heartbeat_default_interval_is_lease_duration_over_three():
    heartbeat = LeaseHeartbeat(
        pool=None, task_id=1, worker_id="w1", lease_duration=timedelta(seconds=30)
    )
    assert heartbeat._interval_seconds == 10.0


# ---------------------------------------------------------------------------
# fencing stops the heartbeat, never attempts cancellation
# ---------------------------------------------------------------------------


def test_heartbeat_stops_and_marks_lost_when_fenced_out(db):
    task_id = _claim(db, worker_id="w1")
    # Simulate the lease being cleared out from under this worker (e.g. by
    # the reaper reclaiming it, or -- simplest deterministic trigger here
    # -- the task simply completing).
    persistence.report_task_success(db, task_id=task_id, worker_id="w1", now=NOW)

    heartbeat = LeaseHeartbeat(
        db,
        task_id=task_id,
        worker_id="w1",
        lease_duration=LEASE,
        interval_seconds=0.05,
        clock=_synced_clock(),
    )
    heartbeat.start()
    try:
        deadline = time.monotonic() + 2.0
        while not heartbeat.lost and time.monotonic() < deadline:
            time.sleep(0.02)
        assert heartbeat.lost is True
    finally:
        heartbeat.stop(timeout=2.0)


def test_heartbeat_does_not_touch_state_or_attempt_count(db):
    task_id = _claim(db)
    heartbeat = LeaseHeartbeat(
        db,
        task_id=task_id,
        worker_id="w1",
        lease_duration=LEASE,
        interval_seconds=0.05,
        clock=_synced_clock(),
    )
    heartbeat.start()
    time.sleep(0.2)
    heartbeat.stop(timeout=2.0)

    with db.connection() as conn:
        row = conn.execute(
            "SELECT state, attempt_count FROM tasks WHERE id = %s", (task_id,)
        ).fetchone()
    assert row["state"] == "RUNNING"
    assert row["attempt_count"] == 1
    assert heartbeat.lost is False  # confirms renewal was genuinely succeeding throughout


# ---------------------------------------------------------------------------
# transient DB failures retry, never conclude the lease is lost
# ---------------------------------------------------------------------------


def test_transient_renewal_failure_is_retried_not_treated_as_lost(db, monkeypatch):
    task_id = _claim(db)

    call_count = {"n": 0}
    real_renew_lease = persistence.renew_lease

    def _flaky(pool, **kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            raise RuntimeError("simulated transient connection error")
        return real_renew_lease(pool, **kwargs)

    monkeypatch.setattr(persistence, "renew_lease", _flaky)

    heartbeat = LeaseHeartbeat(
        db,
        task_id=task_id,
        worker_id="w1",
        lease_duration=LEASE,
        interval_seconds=0.05,
        clock=_synced_clock(),
    )
    heartbeat.start()
    try:
        deadline = time.monotonic() + 2.0
        while call_count["n"] < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        # Give the 3rd (real, successful) call a moment to complete and
        # for heartbeat.lost to reflect its outcome.
        time.sleep(0.1)
        assert call_count["n"] >= 3
        # Must NOT have concluded the lease was lost just because earlier
        # calls raised -- the 3rd call goes through to the real,
        # synchronized-clock renew_lease and genuinely succeeds.
        assert heartbeat.lost is False
    finally:
        heartbeat.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# no cancellation: this module never touches anything but the lease row
# ---------------------------------------------------------------------------


def test_heartbeat_never_cancels_or_interrupts_anything(db):
    """There is no cancellation mechanism to test the absence of directly
    -- this test documents/pins the contract: a heartbeat that loses the
    lease sets `.lost` and stops, and does nothing else (no exception
    raised into the caller's thread, no signal, no thread interruption)."""
    task_id = _claim(db, worker_id="w1")
    persistence.report_task_success(db, task_id=task_id, worker_id="w1", now=NOW)

    still_running = {"value": True}

    def _background_work():
        # Simulates a long-running synchronous executor call that must be
        # completely unaffected by the heartbeat losing its lease.
        time.sleep(0.3)
        still_running["value"] = False

    heartbeat = LeaseHeartbeat(
        db,
        task_id=task_id,
        worker_id="w1",
        lease_duration=LEASE,
        interval_seconds=0.02,
        clock=_synced_clock(),
    )
    heartbeat.start()
    worker_thread = threading.Thread(target=_background_work)
    worker_thread.start()
    worker_thread.join()
    heartbeat.stop(timeout=2.0)

    assert still_running["value"] is False  # the "execution" ran to completion, untouched
    assert heartbeat.lost is True
