"""Heartbeat-phase real-concurrency tests: LeaseHeartbeat racing the
reaper, and LeaseHeartbeat racing task completion. Every thread here uses
a genuinely separate psycopg_pool connection pool (never a shared
in-process connection) -- same real-Postgres-concurrency standard as
tests/test_claiming_concurrency.py and tests/test_reaper.py.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from db import persistence
from db.connection import get_pool
from reaper.reaper import Reaper
from worker.heartbeat import LeaseHeartbeat

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SHORT_LEASE = timedelta(seconds=0.4)


def _synced_clock():
    """A clock anchored at NOW that advances with real elapsed time --
    required because the heartbeat's background thread and the reaper
    loop both pace themselves against real wall-clock waits; see
    tests/test_worker_heartbeat.py's module docstring for the full
    rationale."""
    start_monotonic = time.monotonic()

    def _clock() -> datetime:
        return NOW + timedelta(seconds=time.monotonic() - start_monotonic)

    return _clock


def _make_workflow(db):
    return persistence.create_workflow(db, name="wf")


def _claim_with_short_lease(db, *, worker_id="racer", max_attempts=3):
    wf = _make_workflow(db)
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="noop", max_attempts=max_attempts
    )
    persistence.claim_task_by_id(
        db, task_id=task.id, worker_id=worker_id, now=NOW, lease_duration=SHORT_LEASE
    )
    return task.id


# ---------------------------------------------------------------------------
# heartbeat vs reaper
# ---------------------------------------------------------------------------


def test_heartbeat_racing_reaper_never_crashes_and_settles_consistently(db, db_dsn):
    """A LeaseHeartbeat keeps renewing a short lease in the background
    while a Reaper hammers run_once() concurrently on a genuinely separate
    connection. Regardless of which side wins any individual tick, neither
    side may ever raise, and the task must end up in exactly one
    self-consistent final state."""
    task_id = _claim_with_short_lease(db)

    errors: list[BaseException] = []
    lock = threading.Lock()
    clock = _synced_clock()

    def heartbeat_runner():
        local_pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            heartbeat = LeaseHeartbeat(
                local_pool,
                task_id=task_id,
                worker_id="racer",
                lease_duration=SHORT_LEASE,
                interval_seconds=0.05,
                clock=clock,
            )
            heartbeat.start()
            time.sleep(1.0)
            heartbeat.stop(timeout=2.0)
        except BaseException as exc:  # noqa: BLE001 - the whole point of this test
            with lock:
                errors.append(exc)
        finally:
            local_pool.close()

    def reaper_runner():
        local_pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            reaper = Reaper(local_pool, clock=clock)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                try:
                    reaper.run_once()
                except BaseException as exc:  # noqa: BLE001
                    with lock:
                        errors.append(exc)
                time.sleep(0.03)
        finally:
            local_pool.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(heartbeat_runner), executor.submit(reaper_runner)]
        for future in as_completed(futures):
            future.result()

    assert errors == []  # neither side ever raised, regardless of who won

    with db.connection() as conn:
        row = conn.execute(
            "SELECT state, lease_owner, attempt_count FROM tasks WHERE id = %s", (task_id,)
        ).fetchone()
    # Exactly one self-consistent outcome:
    #  - heartbeat kept winning -> still RUNNING, still owned by "racer"
    #  - reaper won at some point -> reclaimed to READY (attempts remain)
    if row["state"] == "RUNNING":
        assert row["lease_owner"] == "racer"
    else:
        assert row["state"] == "READY"
        assert row["lease_owner"] is None


# ---------------------------------------------------------------------------
# heartbeat vs completion
# ---------------------------------------------------------------------------


def test_heartbeat_racing_completion_never_crashes_and_settles_consistently(db, db_dsn):
    """A LeaseHeartbeat keeps renewing while, concurrently on a separate
    connection, the same worker reports success. Heartbeat must never
    block or corrupt the completion report, and must never crash even
    though it may keep calling renew_lease briefly after the task is
    already done (each such call is expected to return None and set
    heartbeat.lost)."""
    task_id = _claim_with_short_lease(db)

    errors: list[BaseException] = []
    lock = threading.Lock()
    clock = _synced_clock()

    def heartbeat_runner():
        local_pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            heartbeat = LeaseHeartbeat(
                local_pool,
                task_id=task_id,
                worker_id="racer",
                lease_duration=SHORT_LEASE,
                interval_seconds=0.02,
                clock=clock,
            )
            heartbeat.start()
            time.sleep(0.5)
            heartbeat.stop(timeout=2.0)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)
        finally:
            local_pool.close()

    def completion_runner():
        local_pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            time.sleep(0.1)  # let a couple of heartbeats land first
            persistence.report_task_success(
                local_pool, task_id=task_id, worker_id="racer", now=clock()
            )
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)
        finally:
            local_pool.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(heartbeat_runner), executor.submit(completion_runner)]
        for future in as_completed(futures):
            future.result()

    assert errors == []

    with db.connection() as conn:
        row = conn.execute(
            "SELECT state, lease_owner FROM tasks WHERE id = %s", (task_id,)
        ).fetchone()
    assert row["state"] == "SUCCEEDED"
    assert row["lease_owner"] is None
