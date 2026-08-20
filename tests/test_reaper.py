"""Phase 5: Reaper tests. Real PostgreSQL throughout -- lease expiry,
outbox side effects, and the reaper-vs-worker race are all genuinely
transactional/concurrent concerns that must not be mocked."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import pytest

from core.transitions import TaskState, TransitionError
from db import persistence
from db.connection import get_pool
from reaper.config import ReaperConfig
from reaper.reaper import Reaper

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SHORT_LEASE = timedelta(seconds=1)
AFTER_EXPIRY = NOW + SHORT_LEASE + timedelta(seconds=1)


def _make_workflow(db):
    return persistence.create_workflow(db, name="wf")


def _claim_with_short_lease(db, task_id, *, worker_id="w1"):
    return persistence.claim_task_by_id(
        db, task_id=task_id, worker_id=worker_id, now=NOW, lease_duration=SHORT_LEASE
    )


def _reaper(db, *, clock=lambda: AFTER_EXPIRY, config=None):
    return Reaper(db, config=config, clock=clock)


def _outbox_count(db) -> int:
    with db.connection() as conn:
        return conn.execute("SELECT count(*) AS n FROM outbox_events").fetchone()["n"]


# ---------------------------------------------------------------------------
# discovery + reclaim
# ---------------------------------------------------------------------------


def test_reclaims_expired_lease_to_ready_when_attempts_remain(db):
    wf = _make_workflow(db)
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="noop", max_attempts=3
    )
    _claim_with_short_lease(db, task.id)

    result = _reaper(db).run_once()

    assert result.candidates == 1
    assert result.reclaimed == 1
    assert result.lost_races == 0
    assert result.errors == 0

    with db.connection() as conn:
        row = conn.execute(
            "SELECT state, lease_owner FROM tasks WHERE id = %s", (task.id,)
        ).fetchone()
    assert row["state"] == "READY"
    assert row["lease_owner"] is None


def test_reclaims_expired_lease_to_failed_terminal_when_exhausted(db):
    wf = _make_workflow(db)
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="noop", max_attempts=1
    )
    _claim_with_short_lease(db, task.id)

    result = _reaper(db).run_once()

    assert result.reclaimed == 1
    with db.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (task.id,)).fetchone()
    assert row["state"] == "FAILED_TERMINAL"


def test_terminal_reclaim_cascades_skip_to_dependents(db):
    wf = _make_workflow(db)
    parent = persistence.create_task(
        db, workflow_id=wf["id"], name="parent", task_type="noop", max_attempts=1
    )
    child = persistence.create_task(
        db, workflow_id=wf["id"], name="child", task_type="noop", depends_on_ids=[parent.id]
    )
    _claim_with_short_lease(db, parent.id)

    _reaper(db).run_once()

    with db.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (child.id,)).fetchone()
    assert row["state"] == "SKIPPED"


def test_ignores_running_task_with_unexpired_lease(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    persistence.claim_task_by_id(
        db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=timedelta(minutes=5)
    )

    # "now" is still before the (long) lease's expiry.
    result = _reaper(db, clock=lambda: NOW + timedelta(seconds=1)).run_once()

    assert result.candidates == 0
    assert result.reclaimed == 0
    with db.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (task.id,)).fetchone()
    assert row["state"] == "RUNNING"


def test_ignores_non_running_tasks(db):
    wf = _make_workflow(db)
    persistence.create_task(db, workflow_id=wf["id"], name="ready", task_type="noop")
    dep = persistence.create_task(db, workflow_id=wf["id"], name="dep", task_type="noop")
    persistence.create_task(
        db, workflow_id=wf["id"], name="pending", task_type="noop", depends_on_ids=[dep.id]
    )

    result = _reaper(db).run_once()

    assert result.candidates == 0


# ---------------------------------------------------------------------------
# outbox invariant preserved through the reaper
# ---------------------------------------------------------------------------


def test_retry_reclaim_creates_exactly_one_outbox_event(db):
    wf = _make_workflow(db)
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="noop", max_attempts=3
    )
    _claim_with_short_lease(db, task.id)
    before = _outbox_count(db)  # 1, from creation

    _reaper(db).run_once()

    after = _outbox_count(db)
    assert after == before + 1


def test_terminal_reclaim_creates_no_additional_outbox_event(db):
    wf = _make_workflow(db)
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="noop", max_attempts=1
    )
    _claim_with_short_lease(db, task.id)
    before = _outbox_count(db)

    _reaper(db).run_once()

    after = _outbox_count(db)
    assert after == before  # no new event on terminal failure


# ---------------------------------------------------------------------------
# batch size
# ---------------------------------------------------------------------------


def test_batch_size_is_respected(db):
    wf = _make_workflow(db)
    for i in range(5):
        task = persistence.create_task(
            db, workflow_id=wf["id"], name=f"t{i}", task_type="noop", max_attempts=3
        )
        _claim_with_short_lease(db, task.id, worker_id=f"w{i}")

    result = _reaper(db, config=ReaperConfig(batch_size=2)).run_once()
    assert result.candidates == 2
    assert result.reclaimed == 2

    second = _reaper(db, config=ReaperConfig(batch_size=2)).run_once()
    assert second.candidates == 2

    third = _reaper(db, config=ReaperConfig(batch_size=2)).run_once()
    assert third.candidates == 1


# ---------------------------------------------------------------------------
# lost-race handling: TransitionError must never crash the loop
# ---------------------------------------------------------------------------


def test_transition_error_from_expire_lease_is_caught_as_lost_race(db, monkeypatch):
    wf = _make_workflow(db)
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="noop", max_attempts=3
    )
    _claim_with_short_lease(db, task.id)

    real_expire_lease = persistence.expire_lease

    def _raises_transition_error(*args, **kwargs):
        raise TransitionError("simulated lost race: task already resolved elsewhere")

    monkeypatch.setattr(persistence, "expire_lease", _raises_transition_error)

    result = _reaper(db).run_once()

    assert result.candidates == 1
    assert result.reclaimed == 0
    assert result.lost_races == 1
    assert result.errors == 0  # a lost race is NOT counted as an error

    monkeypatch.setattr(persistence, "expire_lease", real_expire_lease)


def test_run_once_continues_past_a_lost_race_to_remaining_candidates(db, monkeypatch):
    wf = _make_workflow(db)
    losing_task = persistence.create_task(
        db, workflow_id=wf["id"], name="loses", task_type="noop", max_attempts=3
    )
    winning_task = persistence.create_task(
        db, workflow_id=wf["id"], name="wins", task_type="noop", max_attempts=3
    )
    _claim_with_short_lease(db, losing_task.id, worker_id="w1")
    _claim_with_short_lease(db, winning_task.id, worker_id="w2")

    real_expire_lease = persistence.expire_lease

    def _flaky(pool, *, task_id, now):
        if task_id == losing_task.id:
            raise TransitionError("simulated lost race")
        return real_expire_lease(pool, task_id=task_id, now=now)

    monkeypatch.setattr(persistence, "expire_lease", _flaky)

    result = _reaper(db).run_once()

    assert result.candidates == 2
    assert result.lost_races == 1
    assert result.reclaimed == 1

    with db.connection() as conn:
        row = conn.execute(
            "SELECT state FROM tasks WHERE id = %s", (winning_task.id,)
        ).fetchone()
    assert row["state"] == "READY"


def test_generic_exception_from_expire_lease_is_counted_as_error_not_lost_race(db, monkeypatch):
    """Distinct from the TransitionError (lost-race) tests above: this
    exercises Reaper.run_once()'s OTHER per-candidate except clause --
    an unexpected, non-TransitionError failure (e.g. a transient DB error)
    on exactly one candidate. Must be counted as an error, must NOT be
    counted as a lost race, must NOT propagate out of run_once(), and must
    NOT prevent the remaining candidate in the same batch from being
    processed normally. This is the inner per-candidate `except Exception`
    branch in reaper/reaper.py -- distinct from run_forever's outer
    discovery-failure handling, which test_run_forever_backs_off_on_repeated_errors
    covers separately."""
    wf = _make_workflow(db)
    failing_task = persistence.create_task(
        db, workflow_id=wf["id"], name="fails", task_type="noop", max_attempts=3
    )
    succeeding_task = persistence.create_task(
        db, workflow_id=wf["id"], name="succeeds", task_type="noop", max_attempts=3
    )
    _claim_with_short_lease(db, failing_task.id, worker_id="w1")
    _claim_with_short_lease(db, succeeding_task.id, worker_id="w2")

    real_expire_lease = persistence.expire_lease

    def _flaky(pool, *, task_id, now):
        if task_id == failing_task.id:
            raise RuntimeError("simulated transient DB error")
        return real_expire_lease(pool, task_id=task_id, now=now)

    monkeypatch.setattr(persistence, "expire_lease", _flaky)

    result = _reaper(db).run_once()  # must not raise

    assert result.candidates == 2
    assert result.errors == 1
    assert result.lost_races == 0  # a generic error is NOT a lost race
    assert result.reclaimed == 1  # the other candidate was still processed

    with db.connection() as conn:
        failing_row = conn.execute(
            "SELECT state, lease_owner FROM tasks WHERE id = %s", (failing_task.id,)
        ).fetchone()
        succeeding_row = conn.execute(
            "SELECT state FROM tasks WHERE id = %s", (succeeding_task.id,)
        ).fetchone()
    # The failing candidate's row is untouched -- expire_lease raised
    # before ever writing (real expire_lease wasn't even called for it).
    assert failing_row["state"] == "RUNNING"
    assert failing_row["lease_owner"] == "w1"
    # The other candidate reached READY normally, via the real
    # expire_lease call, unaffected by its sibling's failure.
    assert succeeding_row["state"] == "READY"


# ---------------------------------------------------------------------------
# run_forever backoff
# ---------------------------------------------------------------------------


def test_run_forever_backs_off_on_repeated_errors(db, monkeypatch):
    def _always_raises(*args, **kwargs):
        raise RuntimeError("db connection lost")

    monkeypatch.setattr(persistence, "list_expired_lease_task_ids", _always_raises)

    reaper = _reaper(
        db,
        config=ReaperConfig(
            batch_size=10,
            poll_interval_seconds=0.01,
            initial_backoff_seconds=0.05,
            max_backoff_seconds=0.2,
        ),
    )

    call_count = 0
    max_calls = 4

    def stop_check():
        nonlocal call_count
        call_count += 1
        return call_count > max_calls

    start = time.monotonic()
    reaper.run_forever(stop_check=stop_check)
    elapsed = time.monotonic() - start

    assert elapsed >= 0.3


def test_run_forever_resets_backoff_after_clean_pass(db):
    reaper = _reaper(
        db,
        config=ReaperConfig(
            batch_size=10,
            poll_interval_seconds=0.01,
            initial_backoff_seconds=1.0,
            max_backoff_seconds=5.0,
        ),
    )

    call_count = 0

    def stop_check():
        nonlocal call_count
        call_count += 1
        return call_count > 2

    start = time.monotonic()
    reaper.run_forever(stop_check=stop_check)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0  # never falls into the (much longer) backoff sleep


# ---------------------------------------------------------------------------
# real concurrency: reaper racing a concurrent report_task_success
# ---------------------------------------------------------------------------


def test_reaper_racing_worker_completion_never_crashes_and_settles_consistently(db, db_dsn):
    """Real, genuinely separate connections. One thread repeatedly runs the
    reaper; the other reports success for the same task shortly after its
    lease has expired. Regardless of which side wins, the loop must never
    raise, and the task must end up in exactly one self-consistent final
    state."""
    wf = _make_workflow(db)
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="noop", max_attempts=3
    )
    _claim_with_short_lease(db, task.id, worker_id="racer")

    errors: list[BaseException] = []
    lock = threading.Lock()

    def reaper_loop():
        local_pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            reaper = Reaper(local_pool, clock=lambda: AFTER_EXPIRY)
            for _ in range(20):
                try:
                    reaper.run_once()
                except BaseException as exc:  # noqa: BLE001 - the whole point of this test
                    with lock:
                        errors.append(exc)
        finally:
            local_pool.close()

    def worker_completion():
        local_pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            for _ in range(20):
                try:
                    persistence.report_task_success(
                        local_pool, task_id=task.id, worker_id="racer", now=AFTER_EXPIRY
                    )
                except BaseException as exc:  # noqa: BLE001
                    with lock:
                        errors.append(exc)
        finally:
            local_pool.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reaper_loop), executor.submit(worker_completion)]
        for future in as_completed(futures):
            future.result()

    assert errors == []  # neither side ever raised, regardless of who won

    with db.connection() as conn:
        row = conn.execute(
            "SELECT state, lease_owner FROM tasks WHERE id = %s", (task.id,)
        ).fetchone()
    # Exactly one self-consistent outcome: either the worker's success
    # report won, or the reaper's reclaim won (retry, since attempts
    # remain). Never left RUNNING, never both.
    assert row["state"] in ("SUCCEEDED", "READY")
    assert row["lease_owner"] is None
