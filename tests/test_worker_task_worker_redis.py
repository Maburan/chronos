"""Phase 7: TaskWorker + TaskTypeLimiter wiring tests.

Real PostgreSQL (same ``db`` fixture as the rest of the suite) for
claim/report behavior; a fake ``MessageSource`` (mirrors
tests/test_worker_task_worker.py) and a fake, deterministic limiter (no
real Redis -- see tests/test_redis_limiter_integration.py for real-Redis
behavior) so these tests focus purely on the admission-gate WIRING:
call order, ACK timing, and exactly-once release.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from db import persistence
from worker.executor import TaskExecutorRegistry
from worker.task_worker import TaskWorker

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)


def _make_workflow(db):
    return persistence.create_workflow(db, name="wf", user_id=db.test_user_id)


def _dispatch_body(*, event_id: int, task_id: int, workflow_id: int, task_type: str) -> bytes:
    return json.dumps(
        {
            "event_id": event_id,
            "task_id": task_id,
            "workflow_id": workflow_id,
            "task_type": task_type,
        }
    ).encode("utf-8")


class FakeMessage:
    def __init__(self, body: bytes):
        self.body = body
        self.acked = False
        self.rejected_with_requeue: bool | None = None

    def ack(self) -> None:
        self.acked = True

    def reject(self, *, requeue: bool) -> None:
        self.rejected_with_requeue = requeue


class FakeMessageSource:
    def __init__(self, messages: list[FakeMessage]):
        self._messages = list(messages)

    def next_message(self):
        if not self._messages:
            return None
        return self._messages.pop(0)


class FakeLimiter:
    """Records every acquire()/release() call, in order, and is scripted
    to grant or deny on demand -- deterministic, no real Redis."""

    def __init__(self, *, grant: bool = True):
        self._grant = grant
        self.calls: list[tuple] = []

    def acquire(self, *, task_type: str, worker_id: str, task_id: int) -> bool:
        self.calls.append(("acquire", task_type, worker_id, task_id))
        return self._grant

    def release(self, *, task_type: str, worker_id: str, task_id: int) -> None:
        self.calls.append(("release", task_type, worker_id, task_id))


def _worker(db, source, registry, limiter, *, worker_id="w1", clock=lambda: NOW):
    return TaskWorker(
        db, source, registry, worker_id=worker_id, lease_duration=LEASE, clock=clock, limiter=limiter
    )


# ---------------------------------------------------------------------------
# denied admission -> ACK, no claim, no state change
# ---------------------------------------------------------------------------


def test_denied_admission_acks_and_never_claims(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    executed = []
    registry = TaskExecutorRegistry({"noop": executed.append})
    limiter = FakeLimiter(grant=False)
    message = FakeMessage(
        _dispatch_body(event_id=1, task_id=task.id, workflow_id=wf["id"], task_type="noop")
    )
    worker = _worker(db, FakeMessageSource([message]), registry, limiter)

    worker.run_once()

    assert message.acked is True
    assert executed == []
    assert limiter.calls == [("acquire", "noop", "w1", task.id)]  # no release -- never acquired

    with db.connection() as conn:
        row = conn.execute(
            "SELECT state, attempt_count FROM tasks WHERE id = %s", (task.id,)
        ).fetchone()
    assert row["state"] == "READY"
    assert row["attempt_count"] == 0


def test_denied_admission_never_calls_claim_task_by_id(db, monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("must not attempt a claim when admission is denied")

    monkeypatch.setattr(persistence, "claim_task_by_id", _fail_if_called)

    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    registry = TaskExecutorRegistry({"noop": lambda record: None})
    limiter = FakeLimiter(grant=False)
    message = FakeMessage(
        _dispatch_body(event_id=1, task_id=task.id, workflow_id=wf["id"], task_type="noop")
    )
    worker = _worker(db, FakeMessageSource([message]), registry, limiter)

    worker.run_once()  # would raise via the monkeypatch if claim were attempted


# ---------------------------------------------------------------------------
# admission granted + claim returns None -> release immediately
# ---------------------------------------------------------------------------


def test_granted_admission_but_claim_fails_releases_immediately(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    # Claim it out-of-band first, so the message's own claim attempt fails.
    persistence.claim_task_by_id(
        db, task_id=task.id, worker_id="someone-else", now=NOW, lease_duration=LEASE
    )

    executed = []
    registry = TaskExecutorRegistry({"noop": executed.append})
    limiter = FakeLimiter(grant=True)
    message = FakeMessage(
        _dispatch_body(event_id=1, task_id=task.id, workflow_id=wf["id"], task_type="noop")
    )
    worker = _worker(db, FakeMessageSource([message]), registry, limiter)

    worker.run_once()

    assert message.acked is True
    assert executed == []
    assert limiter.calls == [
        ("acquire", "noop", "w1", task.id),
        ("release", "noop", "w1", task.id),
    ]


# ---------------------------------------------------------------------------
# admission granted + successful execution -> release after report
# ---------------------------------------------------------------------------


def test_granted_admission_successful_execution_releases_after_report(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    registry = TaskExecutorRegistry({"noop": lambda record: None})
    limiter = FakeLimiter(grant=True)
    message = FakeMessage(
        _dispatch_body(event_id=1, task_id=task.id, workflow_id=wf["id"], task_type="noop")
    )
    worker = _worker(db, FakeMessageSource([message]), registry, limiter)

    worker.run_once()

    assert limiter.calls == [
        ("acquire", "noop", "w1", task.id),
        ("release", "noop", "w1", task.id),
    ]
    with db.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (task.id,)).fetchone()
    assert row["state"] == "SUCCEEDED"


# ---------------------------------------------------------------------------
# admission granted + failed execution -> release still happens
# ---------------------------------------------------------------------------


def test_granted_admission_failed_execution_still_releases(db):
    wf = _make_workflow(db)
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="boom", max_attempts=1
    )

    def _boom(record):
        raise RuntimeError("nope")

    registry = TaskExecutorRegistry({"boom": _boom})
    limiter = FakeLimiter(grant=True)
    message = FakeMessage(
        _dispatch_body(event_id=1, task_id=task.id, workflow_id=wf["id"], task_type="boom")
    )
    worker = _worker(db, FakeMessageSource([message]), registry, limiter)

    worker.run_once()

    assert limiter.calls == [
        ("acquire", "boom", "w1", task.id),
        ("release", "boom", "w1", task.id),
    ]
    with db.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (task.id,)).fetchone()
    assert row["state"] == "FAILED_TERMINAL"


# ---------------------------------------------------------------------------
# no limiter configured -> unchanged, pre-Phase-7 behavior
# ---------------------------------------------------------------------------


def test_no_limiter_configured_behaves_exactly_as_before(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    registry = TaskExecutorRegistry({"noop": lambda record: None})
    message = FakeMessage(
        _dispatch_body(event_id=1, task_id=task.id, workflow_id=wf["id"], task_type="noop")
    )
    # No limiter passed at all -- default None.
    worker = TaskWorker(
        db, FakeMessageSource([message]), registry, worker_id="w1", lease_duration=LEASE, clock=lambda: NOW
    )

    worker.run_once()

    with db.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (task.id,)).fetchone()
    assert row["state"] == "SUCCEEDED"
