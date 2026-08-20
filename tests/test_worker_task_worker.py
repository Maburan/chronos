"""Phase 4: TaskWorker tests. Real PostgreSQL (same ``db`` fixture as the
rest of the suite) for claim/report behavior; a fake ``MessageSource`` (no
real RabbitMQ) for message delivery -- mirrors how
tests/test_outbox_relay.py exercises Phase 3's relay against a fake
publisher plus real Postgres."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from core.transitions import TaskState
from db import persistence
from mq.topology import queue_name_for_task_type
from worker.executor import TaskExecutorRegistry
from worker.task_worker import TaskWorker

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)


def _make_workflow(db):
    return persistence.create_workflow(db, name="wf")


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
        self.acked_before_execute: bool | None = None

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


def _worker(db, source, registry, *, worker_id="w1", clock=lambda: NOW):
    return TaskWorker(
        db, source, registry, worker_id=worker_id, lease_duration=LEASE, clock=clock
    )


# ---------------------------------------------------------------------------
# 1/2/7/12/13. happy path: claim, ack, execute, success report
# ---------------------------------------------------------------------------


def test_valid_message_claims_and_executes_task(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    received = []
    registry = TaskExecutorRegistry({"noop": received.append})
    message = FakeMessage(
        _dispatch_body(event_id=1, task_id=task.id, workflow_id=wf["id"], task_type="noop")
    )
    worker = _worker(db, FakeMessageSource([message]), registry)

    handled = worker.run_once()

    assert handled is True
    assert message.acked is True
    assert len(received) == 1
    # 13. executor receives the authoritative claimed TaskRecord
    assert received[0].id == task.id
    assert received[0].task_type == "noop"
    assert received[0].instance.state == TaskState.RUNNING  # state at claim time

    with db.connection() as conn:
        row = conn.execute("SELECT state, lease_owner FROM tasks WHERE id = %s", (task.id,)).fetchone()
    assert row["state"] == "SUCCEEDED"
    assert row["lease_owner"] is None


def test_worker_uses_task_id_from_message_not_some_other_ready_task(db):
    """The exact scenario the targeted-claim design exists to prevent."""
    wf = _make_workflow(db)
    task_42 = persistence.create_task(db, workflow_id=wf["id"], name="forty-two", task_type="noop")
    task_57 = persistence.create_task(db, workflow_id=wf["id"], name="fifty-seven", task_type="noop")

    registry = TaskExecutorRegistry({"noop": lambda record: None})
    message = FakeMessage(
        _dispatch_body(event_id=1, task_id=task_42.id, workflow_id=wf["id"], task_type="noop")
    )
    worker = _worker(db, FakeMessageSource([message]), registry)
    worker.run_once()

    with db.connection() as conn:
        row_42 = conn.execute("SELECT state FROM tasks WHERE id = %s", (task_42.id,)).fetchone()
        row_57 = conn.execute("SELECT state FROM tasks WHERE id = %s", (task_57.id,)).fetchone()
    assert row_42["state"] == "SUCCEEDED"
    assert row_57["state"] == "READY"  # untouched


def test_worker_id_passed_correctly_to_claim(db):
    """lease_owner is cleared again once report_task_success runs, so the
    claimed lease_owner is observed from inside the executor -- the
    moment it's guaranteed to still be set to the configured worker_id."""
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    seen_lease_owner = []
    registry = TaskExecutorRegistry(
        {"noop": lambda record: seen_lease_owner.append(record.instance.lease_owner)}
    )
    message = FakeMessage(
        _dispatch_body(event_id=1, task_id=task.id, workflow_id=wf["id"], task_type="noop")
    )
    worker = _worker(db, FakeMessageSource([message]), registry, worker_id="worker-abc")
    worker.run_once()

    assert seen_lease_owner == ["worker-abc"]


# ---------------------------------------------------------------------------
# 4/8. claim returns None -> ack, no execution
# ---------------------------------------------------------------------------


def test_claim_none_acks_and_does_not_execute(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    # Claim it out-of-band first, so the message's claim attempt fails.
    persistence.claim_task_by_id(db, task_id=task.id, worker_id="someone-else", now=NOW, lease_duration=LEASE)

    executed = []
    registry = TaskExecutorRegistry({"noop": executed.append})
    message = FakeMessage(
        _dispatch_body(event_id=1, task_id=task.id, workflow_id=wf["id"], task_type="noop")
    )
    worker = _worker(db, FakeMessageSource([message]), registry)
    worker.run_once()

    assert message.acked is True
    assert executed == []


def test_nonexistent_task_id_acks_and_does_nothing(db):
    registry = TaskExecutorRegistry({"noop": lambda record: None})
    message = FakeMessage(
        _dispatch_body(event_id=1, task_id=999999, workflow_id=1, task_type="noop")
    )
    worker = _worker(db, FakeMessageSource([message]), registry)
    worker.run_once()

    assert message.acked is True


def test_not_claimable_log_line_includes_event_id(db, caplog):
    """Phase 10: event_id must appear in the 'not claimable' log line --
    the correlation point tying this worker log line back to the relay's
    matching 'confirmed publish' log line for the same event."""
    registry = TaskExecutorRegistry({"noop": lambda record: None})
    message = FakeMessage(
        _dispatch_body(event_id=42, task_id=999999, workflow_id=1, task_type="noop")
    )
    worker = _worker(db, FakeMessageSource([message]), registry)

    with caplog.at_level("INFO", logger="worker.task_worker"):
        worker.run_once()

    matching = [r for r in caplog.records if "event_id=42" in r.message]
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# 5. duplicate dispatch -> only one execution
# ---------------------------------------------------------------------------


def test_duplicate_dispatch_only_one_worker_executes(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    executed = []
    registry = TaskExecutorRegistry({"noop": executed.append})

    body = _dispatch_body(event_id=1, task_id=task.id, workflow_id=wf["id"], task_type="noop")
    message_1 = FakeMessage(body)
    message_2 = FakeMessage(body)  # simulates the same event delivered twice

    worker = _worker(db, FakeMessageSource([message_1, message_2]), registry)
    worker.run_once()
    worker.run_once()

    assert message_1.acked is True
    assert message_2.acked is True
    assert len(executed) == 1  # only the first claim succeeded


# ---------------------------------------------------------------------------
# 6. malformed message rejected safely
# ---------------------------------------------------------------------------


def test_malformed_message_is_rejected_not_acked(db):
    executed = []
    registry = TaskExecutorRegistry({"noop": executed.append})
    message = FakeMessage(b"not valid json {{{")
    worker = _worker(db, FakeMessageSource([message]), registry)

    handled = worker.run_once()

    assert handled is True
    assert message.acked is False
    assert message.rejected_with_requeue is False
    assert executed == []


def test_malformed_message_missing_task_id_is_rejected(db):
    registry = TaskExecutorRegistry({"noop": lambda record: None})
    body = json.dumps({"event_id": 1, "workflow_id": 2, "task_type": "noop"}).encode()
    message = FakeMessage(body)
    worker = _worker(db, FakeMessageSource([message]), registry)

    worker.run_once()

    assert message.acked is False
    assert message.rejected_with_requeue is False


# ---------------------------------------------------------------------------
# 8/9/11. failed execution -> report_task_failure, retry semantics preserved
# ---------------------------------------------------------------------------


def test_execution_failure_reports_task_failure_and_retries(db):
    wf = _make_workflow(db)
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="boom", max_attempts=2
    )

    def _boom(record):
        raise RuntimeError("execution blew up")

    registry = TaskExecutorRegistry({"boom": _boom})
    message = FakeMessage(
        _dispatch_body(event_id=1, task_id=task.id, workflow_id=wf["id"], task_type="boom")
    )
    worker = _worker(db, FakeMessageSource([message]), registry)
    worker.run_once()

    with db.connection() as conn:
        row = conn.execute(
            "SELECT state, attempt_count FROM tasks WHERE id = %s", (task.id,)
        ).fetchone()
    assert row["state"] == "READY"  # attempts remain -> retried
    assert row["attempt_count"] == 1


def test_execution_failure_terminal_when_attempts_exhausted(db):
    wf = _make_workflow(db)
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="boom", max_attempts=1
    )

    def _boom(record):
        raise RuntimeError("execution blew up")

    registry = TaskExecutorRegistry({"boom": _boom})
    message = FakeMessage(
        _dispatch_body(event_id=1, task_id=task.id, workflow_id=wf["id"], task_type="boom")
    )
    worker = _worker(db, FakeMessageSource([message]), registry)
    worker.run_once()

    with db.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (task.id,)).fetchone()
    assert row["state"] == "FAILED_TERMINAL"


def test_unregistered_task_type_is_treated_as_execution_failure(db):
    wf = _make_workflow(db)
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="mystery", max_attempts=1
    )

    registry = TaskExecutorRegistry({"noop": lambda record: None})  # "mystery" not registered
    message = FakeMessage(
        _dispatch_body(event_id=1, task_id=task.id, workflow_id=wf["id"], task_type="mystery")
    )
    worker = _worker(db, FakeMessageSource([message]), registry)
    worker.run_once()

    with db.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (task.id,)).fetchone()
    assert row["state"] == "FAILED_TERMINAL"  # max_attempts=1, no retry available


def test_execution_failure_does_not_cause_a_second_claim(db):
    wf = _make_workflow(db)
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="boom", max_attempts=5
    )

    def _boom(record):
        raise RuntimeError("execution blew up")

    registry = TaskExecutorRegistry({"boom": _boom})
    message = FakeMessage(
        _dispatch_body(event_id=1, task_id=task.id, workflow_id=wf["id"], task_type="boom")
    )
    worker = _worker(db, FakeMessageSource([message]), registry)
    worker.run_once()

    with db.connection() as conn:
        row = conn.execute(
            "SELECT attempt_count FROM tasks WHERE id = %s", (task.id,)
        ).fetchone()
    # attempt_count increments exactly once per claim -- a single failed
    # execution must not have triggered a second, redundant claim attempt.
    assert row["attempt_count"] == 1


# ---------------------------------------------------------------------------
# 10. ACK happens before execution completes
# ---------------------------------------------------------------------------


def test_ack_happens_before_execution_completes(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    message = FakeMessage(
        _dispatch_body(event_id=1, task_id=task.id, workflow_id=wf["id"], task_type="noop")
    )

    def _executor(record):
        # At the moment execution starts, the message must already be
        # acked -- this is the whole point of the ACK-before-execute rule.
        message.acked_before_execute = message.acked

    registry = TaskExecutorRegistry({"noop": _executor})
    worker = _worker(db, FakeMessageSource([message]), registry)
    worker.run_once()

    assert message.acked_before_execute is True


# ---------------------------------------------------------------------------
# 3. worker never calls the untargeted claim_task()
# ---------------------------------------------------------------------------


def test_worker_never_calls_generic_claim_task(db, monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("TaskWorker must never call the untargeted claim_task()")

    monkeypatch.setattr(persistence, "claim_task", _fail_if_called)

    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    registry = TaskExecutorRegistry({"noop": lambda record: None})

    messages = [
        FakeMessage(_dispatch_body(event_id=1, task_id=task.id, workflow_id=wf["id"], task_type="noop")),
        FakeMessage(_dispatch_body(event_id=2, task_id=999999, workflow_id=wf["id"], task_type="noop")),
        FakeMessage(b"malformed"),
    ]
    worker = _worker(db, FakeMessageSource(messages), registry)
    worker.run_once()
    worker.run_once()
    worker.run_once()
    # If claim_task had been called anywhere above, the monkeypatched
    # AssertionError would have propagated out of run_once already.


# ---------------------------------------------------------------------------
# 14. task_type routing / queue ownership
# ---------------------------------------------------------------------------


def test_worker_task_type_maps_to_the_expected_queue_name():
    """Documents/pins the wiring a real deployment uses: a worker
    configured for task_type='send_email' consumes exactly the queue
    Phase 3's topology declared for that type -- see mq/topology.py."""
    assert queue_name_for_task_type("send_email", exchange="chronos.tasks") == "chronos.tasks.send_email"


# ---------------------------------------------------------------------------
# construction validation
# ---------------------------------------------------------------------------


def test_worker_requires_worker_id(db):
    registry = TaskExecutorRegistry({})
    with pytest.raises(ValueError):
        TaskWorker(db, FakeMessageSource([]), registry, worker_id="", lease_duration=LEASE)


def test_run_once_returns_false_when_no_message_available(db):
    registry = TaskExecutorRegistry({})
    worker = _worker(db, FakeMessageSource([]), registry)
    assert worker.run_once() is False


def test_run_forever_stops_on_stop_check(db):
    registry = TaskExecutorRegistry({})
    worker = _worker(db, FakeMessageSource([]), registry)

    calls = {"n": 0}

    def stop_check():
        calls["n"] += 1
        return calls["n"] > 2

    worker.run_forever(stop_check=stop_check, idle_sleep_seconds=0.001)
    assert calls["n"] == 3


# ---------------------------------------------------------------------------
# on_iteration (Phase 10 -- operational readiness)
# ---------------------------------------------------------------------------


def test_run_forever_calls_on_iteration_with_each_result(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    registry = TaskExecutorRegistry({"noop": lambda record: None})
    message = FakeMessage(
        _dispatch_body(event_id=1, task_id=task.id, workflow_id=wf["id"], task_type="noop")
    )
    worker = _worker(db, FakeMessageSource([message, None]), registry)

    results = []
    call_count = 0

    def stop_check():
        nonlocal call_count
        call_count += 1
        return call_count > 2

    worker.run_forever(stop_check=stop_check, idle_sleep_seconds=0.001, on_iteration=results.append)

    assert len(results) == 2
    assert results[0] is True  # the seeded message was handled
    assert results[1] is False  # no further message available


def test_run_forever_without_on_iteration_behaves_exactly_as_before(db):
    """Backward compatibility: omitting on_iteration entirely (the
    default, None) must not change anything about run_forever's existing
    behavior -- it simply isn't called."""
    registry = TaskExecutorRegistry({})
    worker = _worker(db, FakeMessageSource([]), registry)

    calls = {"n": 0}

    def stop_check():
        calls["n"] += 1
        return calls["n"] > 2

    worker.run_forever(stop_check=stop_check, idle_sleep_seconds=0.001)  # no on_iteration
    assert calls["n"] == 3
