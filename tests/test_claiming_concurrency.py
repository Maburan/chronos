"""Real multi-connection concurrency tests. Every worker in these tests
uses a genuinely separate psycopg connection -- in-process threads sharing
one connection would not exercise real Postgres row locking."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from core.transitions import TaskState
from db import persistence
from db.connection import get_pool

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)


def _seed_ready_tasks(db, n: int, *, max_attempts: int = 1) -> list[int]:
    wf = persistence.create_workflow(db, name="concurrency-wf")
    ids = []
    for i in range(n):
        record = persistence.create_task(
            db, workflow_id=wf["id"], name=f"t{i}", task_type="noop", max_attempts=max_attempts
        )
        ids.append(record.id)
    return ids


# ---------------------------------------------------------------------------
# 1. no double-claim, no lost tasks
# ---------------------------------------------------------------------------


def test_no_double_claim_across_concurrent_workers(db, db_dsn):
    n_tasks = 50
    n_workers = 10
    _seed_ready_tasks(db, n_tasks)

    claimed_pairs: list[tuple[int, str]] = []
    lock = threading.Lock()

    def worker_loop(worker_id: str):
        local_pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            claimed_here = []
            while True:
                record = persistence.claim_task(
                    local_pool, worker_id=worker_id, now=NOW, lease_duration=LEASE
                )
                if record is None:
                    break
                claimed_here.append((record.id, worker_id))
            return claimed_here
        finally:
            local_pool.close()

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = [executor.submit(worker_loop, f"w{i}") for i in range(n_workers)]
        for future in as_completed(futures):
            with lock:
                claimed_pairs.extend(future.result())

    claimed_task_ids = [task_id for task_id, _ in claimed_pairs]
    assert len(claimed_task_ids) == n_tasks
    assert len(set(claimed_task_ids)) == n_tasks  # every id claimed exactly once


# ---------------------------------------------------------------------------
# 2. SKIP LOCKED skips rather than blocks
# ---------------------------------------------------------------------------


def test_skip_locked_does_not_block_concurrent_claimants(db, db_dsn):
    _seed_ready_tasks(db, 2)

    conn_a = psycopg.connect(db_dsn, row_factory=psycopg.rows.dict_row)
    conn_b = psycopg.connect(db_dsn, row_factory=psycopg.rows.dict_row)
    try:
        conn_a.execute("BEGIN")
        conn_b.execute("BEGIN")

        row_a = conn_a.execute(
            "SELECT id FROM tasks WHERE state = 'READY' ORDER BY created_at "
            "FOR UPDATE SKIP LOCKED LIMIT 1"
        ).fetchone()
        row_b = conn_b.execute(
            "SELECT id FROM tasks WHERE state = 'READY' ORDER BY created_at "
            "FOR UPDATE SKIP LOCKED LIMIT 1"
        ).fetchone()

        assert row_a is not None and row_b is not None
        assert row_a["id"] != row_b["id"]
    finally:
        conn_a.rollback()
        conn_b.rollback()
        conn_a.close()
        conn_b.close()


# ---------------------------------------------------------------------------
# 3. rollback releases the lock
# ---------------------------------------------------------------------------


def test_rollback_releases_lock_for_next_claimant(db, db_dsn):
    task_ids = _seed_ready_tasks(db, 1)

    conn = psycopg.connect(db_dsn, row_factory=psycopg.rows.dict_row)
    try:
        conn.execute("BEGIN")
        conn.execute(
            "SELECT id FROM tasks WHERE state = 'READY' "
            "FOR UPDATE SKIP LOCKED LIMIT 1"
        ).fetchone()
        conn.rollback()  # simulate a worker dying mid-claim
    finally:
        conn.close()

    with db.connection() as check_conn:
        row = check_conn.execute(
            "SELECT state FROM tasks WHERE id = %s", (task_ids[0],)
        ).fetchone()
    assert row["state"] == "READY"

    claimed = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    assert claimed is not None
    assert claimed.id == task_ids[0]


# ---------------------------------------------------------------------------
# 4. attempt_count never exceeds max_attempts under contention
# ---------------------------------------------------------------------------


def test_attempt_count_never_exceeds_max_under_contention(db, db_dsn):
    max_attempts = 3
    task_ids = _seed_ready_tasks(db, 1, max_attempts=max_attempts)
    task_id = task_ids[0]

    def hammer(worker_id: str):
        local_pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            for _ in range(max_attempts + 5):
                claimed = persistence.claim_task(
                    local_pool, worker_id=worker_id, now=NOW, lease_duration=LEASE
                )
                if claimed is None:
                    continue
                persistence.report_task_failure(
                    local_pool, task_id=claimed.id, worker_id=worker_id, now=NOW
                )
        finally:
            local_pool.close()

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(hammer, f"w{i}") for i in range(5)]
        for future in as_completed(futures):
            future.result()

    with db.connection() as conn:
        row = conn.execute(
            "SELECT state, attempt_count, max_attempts FROM tasks WHERE id = %s", (task_id,)
        ).fetchone()
    assert row["state"] == "FAILED_TERMINAL"
    assert row["attempt_count"] == max_attempts
    assert row["attempt_count"] <= row["max_attempts"]


# ---------------------------------------------------------------------------
# 5. stale-report fencing at the DB layer
# ---------------------------------------------------------------------------


def test_db_level_stale_report_fencing(db, db_dsn):
    task_ids = _seed_ready_tasks(db, 1, max_attempts=3)
    task_id = task_ids[0]

    claimed_a = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    assert claimed_a.instance.lease_owner == "w1"

    after_expiry = NOW + LEASE + timedelta(seconds=1)
    reclaimed = persistence.expire_lease(db, task_id=task_id, now=after_expiry)
    assert reclaimed.record.instance.state == TaskState.READY

    claimed_b = persistence.claim_task(db, worker_id="w2", now=after_expiry, lease_duration=LEASE)
    assert claimed_b is not None
    assert claimed_b.instance.lease_owner == "w2"

    # w1's zombie success report arrives after w2 has already reclaimed it.
    stale = persistence.report_task_success(db, task_id=task_id, worker_id="w1", now=after_expiry)
    assert stale.applied is False

    with db.connection() as conn:
        row = conn.execute("SELECT lease_owner, state FROM tasks WHERE id = %s", (task_id,)).fetchone()
    assert row["lease_owner"] == "w2"
    assert row["state"] == "RUNNING"


# ---------------------------------------------------------------------------
# 6. concurrent fan-in completion
# ---------------------------------------------------------------------------


def test_concurrent_fan_in_completion_promotes_downstream_exactly_once(db, db_dsn):
    wf = persistence.create_workflow(db, name="fan-in-wf")
    x = persistence.create_task(db, workflow_id=wf["id"], name="x", task_type="noop")
    y = persistence.create_task(db, workflow_id=wf["id"], name="y", task_type="noop")
    z = persistence.create_task(
        db, workflow_id=wf["id"], name="z", task_type="noop", depends_on_ids=[x.id, y.id]
    )

    def complete(task_id: int, worker_id: str):
        local_pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            claimed = persistence.claim_task(
                local_pool, worker_id=worker_id, now=NOW, lease_duration=LEASE
            )
            persistence.report_task_success(
                local_pool, task_id=claimed.id, worker_id=worker_id, now=NOW
            )
        finally:
            local_pool.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(complete, x.id, "w1"),
            executor.submit(complete, y.id, "w2"),
        ]
        for future in as_completed(futures):
            future.result()  # re-raises; asserts no deadlock error

    with db.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (z.id,)).fetchone()
    assert row["state"] == "READY"


# ---------------------------------------------------------------------------
# 7. invariant fuzz pass
# ---------------------------------------------------------------------------


def test_invariant_fuzz_pass_under_concurrency(db, db_dsn):
    wf = persistence.create_workflow(db, name="fuzz-wf")
    task_ids = []
    for i in range(15):
        record = persistence.create_task(
            db, workflow_id=wf["id"], name=f"fuzz{i}", task_type="noop", max_attempts=2
        )
        task_ids.append(record.id)

    def churn(worker_id: str):
        local_pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            for _ in range(30):
                claimed = persistence.claim_task(
                    local_pool, worker_id=worker_id, now=NOW, lease_duration=LEASE
                )
                if claimed is None:
                    continue
                if int(claimed.id) % 2 == 0:
                    persistence.report_task_success(
                        local_pool, task_id=claimed.id, worker_id=worker_id, now=NOW
                    )
                else:
                    persistence.report_task_failure(
                        local_pool, task_id=claimed.id, worker_id=worker_id, now=NOW
                    )
        finally:
            local_pool.close()

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(churn, f"w{i}") for i in range(6)]
        for future in as_completed(futures):
            future.result()

    with db.connection() as conn:
        rows = conn.execute(
            "SELECT id, state, attempt_count, max_attempts, lease_owner, lease_expires_at "
            "FROM tasks WHERE id = ANY(%s)",
            (task_ids,),
        ).fetchall()

    for row in rows:
        assert row["attempt_count"] <= row["max_attempts"]
        assert (row["lease_owner"] is None) == (row["lease_expires_at"] is None)
        if row["state"] in ("SUCCEEDED", "FAILED_TERMINAL", "SKIPPED"):
            assert row["lease_owner"] is None
        else:
            assert row["state"] in ("READY", "RUNNING")


# ---------------------------------------------------------------------------
# 5. claim_task_by_id (Phase 4 prerequisite: targeted claim) concurrency
# ---------------------------------------------------------------------------


def test_claim_task_by_id_same_task_exactly_one_winner(db, db_dsn):
    """Two genuinely separate connections racing claim_task_by_id for the
    SAME task_id must produce exactly one successful claim -- the ordinary
    Postgres row-lock semantics claim_task_by_id relies on (no SKIP LOCKED,
    no CTE) instead of claim_task's multi-candidate-row scan."""
    [task_id] = _seed_ready_tasks(db, 1)

    results: list[object] = []
    lock = threading.Lock()

    def attempt_claim(worker_id: str):
        local_pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            return persistence.claim_task_by_id(
                local_pool, task_id=task_id, worker_id=worker_id, now=NOW, lease_duration=LEASE
            )
        finally:
            local_pool.close()

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(attempt_claim, f"w{i}") for i in range(10)]
        for future in as_completed(futures):
            with lock:
                results.append(future.result())

    successful = [r for r in results if r is not None]
    assert len(successful) == 1
    assert successful[0].id == task_id

    with db.connection() as conn:
        row = conn.execute("SELECT state, attempt_count FROM tasks WHERE id = %s", (task_id,)).fetchone()
    assert row["state"] == "RUNNING"
    assert row["attempt_count"] == 1  # never double-incremented by the losing racers


def test_claim_task_by_id_different_tasks_claim_independently(db, db_dsn):
    """Two separate connections targeting DIFFERENT task_ids must not block
    each other and must both succeed -- claim_task_by_id locks only the
    single row it names, never contending with an unrelated task_id."""
    task_ids = _seed_ready_tasks(db, 2)

    def attempt_claim(task_id: int, worker_id: str):
        local_pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            return persistence.claim_task_by_id(
                local_pool, task_id=task_id, worker_id=worker_id, now=NOW, lease_duration=LEASE
            )
        finally:
            local_pool.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(attempt_claim, task_ids[0], "w0"),
            executor.submit(attempt_claim, task_ids[1], "w1"),
        ]
        results = [f.result() for f in as_completed(futures)]

    assert all(r is not None for r in results)
    claimed_ids = sorted(r.id for r in results)
    assert claimed_ids == sorted(task_ids)
