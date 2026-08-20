"""Phase 5: FallbackPoller tests. Real PostgreSQL throughout -- the
poller's whole point is direct, RabbitMQ-free interaction with
PostgreSQL, so there's nothing meaningful to fake here."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import pytest

from core.transitions import TaskState
from db import persistence
from db.connection import get_pool
from worker.executor import TaskExecutorRegistry
from worker.fallback_poller import FallbackPoller

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)


def _make_workflow(db):
    return persistence.create_workflow(db, name="wf")


def _poller(db, registry, *, worker_id="fallback-1", clock=lambda: NOW):
    return FallbackPoller(db, registry, worker_id=worker_id, lease_duration=LEASE, clock=clock)


# ---------------------------------------------------------------------------
# happy path / failure path
# ---------------------------------------------------------------------------


def test_claims_executes_and_reports_success(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    received = []
    registry = TaskExecutorRegistry({"noop": received.append})
    poller = _poller(db, registry)

    handled = poller.run_once()

    assert handled is True
    assert len(received) == 1
    assert received[0].id == task.id

    with db.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (task.id,)).fetchone()
    assert row["state"] == "SUCCEEDED"


def test_execution_failure_reports_task_failure(db):
    wf = _make_workflow(db)
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="boom", max_attempts=1
    )

    def _boom(record):
        raise RuntimeError("nope")

    registry = TaskExecutorRegistry({"boom": _boom})
    poller = _poller(db, registry)
    poller.run_once()

    with db.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (task.id,)).fetchone()
    assert row["state"] == "FAILED_TERMINAL"


def test_run_once_returns_false_when_nothing_ready(db):
    registry = TaskExecutorRegistry({})
    poller = _poller(db, registry)
    assert poller.run_once() is False


def test_worker_id_is_used_for_the_claim(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    seen_lease_owner = []
    registry = TaskExecutorRegistry(
        {"noop": lambda record: seen_lease_owner.append(record.instance.lease_owner)}
    )
    poller = _poller(db, registry, worker_id="fallback-xyz")
    poller.run_once()

    assert seen_lease_owner == ["fallback-xyz"]


def test_requires_worker_id(db):
    registry = TaskExecutorRegistry({})
    with pytest.raises(ValueError):
        FallbackPoller(db, registry, worker_id="", lease_duration=LEASE)


# ---------------------------------------------------------------------------
# never uses the targeted claim -- only the untargeted claim_task()
# ---------------------------------------------------------------------------


def test_fallback_poller_never_calls_claim_task_by_id(db, monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("FallbackPoller must never call claim_task_by_id()")

    monkeypatch.setattr(persistence, "claim_task_by_id", _fail_if_called)

    wf = _make_workflow(db)
    persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    registry = TaskExecutorRegistry({"noop": lambda record: None})
    poller = _poller(db, registry)

    poller.run_once()
    # If claim_task_by_id had been called, the monkeypatched AssertionError
    # would already have propagated out of run_once above.


# ---------------------------------------------------------------------------
# run_forever
# ---------------------------------------------------------------------------


def test_run_forever_stops_on_stop_check(db):
    registry = TaskExecutorRegistry({})
    poller = _poller(db, registry)

    calls = {"n": 0}

    def stop_check():
        calls["n"] += 1
        return calls["n"] > 2

    poller.run_forever(stop_check=stop_check, idle_sleep_seconds=0.001)
    assert calls["n"] == 3


# ---------------------------------------------------------------------------
# real concurrency: FallbackPoller alongside claim_task_by_id() (the
# RabbitMQ worker's path) must never double-claim the same task
# ---------------------------------------------------------------------------


def test_fallback_poller_and_targeted_claim_never_double_claim(db, db_dsn):
    """Seeds N READY tasks. Several threads run FallbackPoller.run_once()
    in a loop (the claim_task() path); several other threads call
    claim_task_by_id() directly against known task_ids (simulating the
    RabbitMQ-driven worker path) concurrently. Every task must end up
    claimed by exactly one side, never both."""
    n_tasks = 40
    wf = persistence.create_workflow(db, name="concurrency-wf")
    task_ids = [
        persistence.create_task(db, workflow_id=wf["id"], name=f"t{i}", task_type="noop").id
        for i in range(n_tasks)
    ]

    claimed_ids: list[int] = []
    lock = threading.Lock()

    def fallback_worker(worker_id: str):
        local_pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            claimed_here = []
            registry = TaskExecutorRegistry(
                {"noop": lambda record: claimed_here.append(record.id)}
            )
            poller = FallbackPoller(
                local_pool, registry, worker_id=worker_id, lease_duration=LEASE
            )
            while poller.run_once():
                pass
            return claimed_here
        finally:
            local_pool.close()

    def targeted_worker(worker_id: str, ids: list[int]):
        local_pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            claimed_here = []
            for task_id in ids:
                claimed = persistence.claim_task_by_id(
                    local_pool, task_id=task_id, worker_id=worker_id, now=NOW, lease_duration=LEASE
                )
                if claimed is not None:
                    claimed_here.append(claimed.id)
            return claimed_here
        finally:
            local_pool.close()

    half = n_tasks // 2
    targeted_ids = task_ids[:half]  # explicitly targeted by "RabbitMQ path" threads

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fallback_worker, f"fallback-{i}") for i in range(4)]
        futures += [
            executor.submit(targeted_worker, f"targeted-{i}", targeted_ids) for i in range(4)
        ]
        for future in as_completed(futures):
            with lock:
                claimed_ids.extend(future.result())

    assert len(claimed_ids) == n_tasks
    assert len(set(claimed_ids)) == n_tasks  # every task claimed exactly once, by exactly one side
