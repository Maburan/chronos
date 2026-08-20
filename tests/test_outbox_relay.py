"""Phase 3: outbox relay tests.

Uses the same real-PostgreSQL fixtures as the rest of the suite (``db``) --
the relay's DB-facing behavior (fetch batch, mark published) is real,
transactional Postgres, never mocked. The RabbitMQ side is a small
controlled fake (``FakePublisher``) implementing
``mq.connection.ConfirmedPublisher`` -- deterministic, no broker required
for this file to run. Real-broker verification lives in
``tests/test_rabbitmq_integration.py``.
"""

from __future__ import annotations

import json
import time

import pytest

from db import persistence
from mq.config import RelayConfig
from relay.outbox_relay import OutboxRelay, RelayIterationResult


def _make_workflow(db):
    return persistence.create_workflow(db, name="wf")


def _outbox_rows(db) -> list[dict]:
    with db.connection() as conn:
        return conn.execute("SELECT * FROM outbox_events ORDER BY created_at, id").fetchall()


class FakePublisher:
    """Controlled ``ConfirmedPublisher``: records every publish call and
    can be scripted to confirm, refuse (return False), or raise per call
    (or uniformly)."""

    def __init__(self, *, confirm: bool = True, raise_exc: Exception | None = None):
        self.calls: list[tuple[str, bytes]] = []
        self._confirm = confirm
        self._raise_exc = raise_exc

    def publish(self, *, routing_key: str, body: bytes) -> bool:
        self.calls.append((routing_key, body))
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._confirm

    def decoded_calls(self) -> list[tuple[str, dict]]:
        return [(rk, json.loads(body)) for rk, body in self.calls]


# ---------------------------------------------------------------------------
# message shape / routing
# ---------------------------------------------------------------------------


def test_publishes_correct_routing_key_and_payload(db):
    wf = _make_workflow(db)
    record = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="send_email"
    )

    publisher = FakePublisher(confirm=True)
    relay = OutboxRelay(db, publisher)
    result = relay.run_once()

    assert result.fetched == 1
    assert result.published == 1
    assert result.errors == 0

    [(routing_key, payload)] = publisher.decoded_calls()
    assert routing_key == "task.send_email"

    [event] = _outbox_rows(db)
    assert payload == {
        "event_id": event["id"],
        "task_id": record.id,
        "workflow_id": wf["id"],
        "task_type": "send_email",
    }


def test_confirmed_publish_logs_correlation_ids(db, caplog):
    """Phase 10: event_id/task_id/task_type must appear together in a log
    line at the moment of confirmed publish -- the one correlation point
    letting this relay log line and the matching worker log line for the
    same event be tied together."""
    wf = _make_workflow(db)
    record = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="send_email"
    )

    publisher = FakePublisher(confirm=True)
    with caplog.at_level("INFO", logger="relay.outbox_relay"):
        OutboxRelay(db, publisher).run_once()

    [event] = _outbox_rows(db)
    matching = [
        r
        for r in caplog.records
        if f"event_id={event['id']}" in r.message
        and f"task_id={record.id}" in r.message
        and "task_type=send_email" in r.message
    ]
    assert len(matching) == 1


def test_message_excludes_authoritative_task_state(db):
    wf = _make_workflow(db)
    persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    publisher = FakePublisher(confirm=True)
    OutboxRelay(db, publisher).run_once()

    [(_, payload)] = publisher.decoded_calls()
    assert set(payload.keys()) == {"event_id", "task_id", "workflow_id", "task_type"}


# ---------------------------------------------------------------------------
# publish confirmation gates published_at
# ---------------------------------------------------------------------------


def test_confirmed_publish_marks_event_published(db):
    wf = _make_workflow(db)
    persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    relay = OutboxRelay(db, FakePublisher(confirm=True))
    relay.run_once()

    [event] = _outbox_rows(db)
    assert event["published_at"] is not None


def test_unconfirmed_publish_leaves_published_at_null(db):
    wf = _make_workflow(db)
    persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    relay = OutboxRelay(db, FakePublisher(confirm=False))
    result = relay.run_once()

    assert result.published == 0
    assert result.errors == 1
    [event] = _outbox_rows(db)
    assert event["published_at"] is None


def test_publish_exception_leaves_published_at_null(db):
    wf = _make_workflow(db)
    persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    relay = OutboxRelay(db, FakePublisher(raise_exc=ConnectionError("broker down")))
    result = relay.run_once()

    assert result.published == 0
    assert result.errors == 1
    [event] = _outbox_rows(db)
    assert event["published_at"] is None


def test_connection_failure_does_not_mark_events_published(db):
    """Simulates a broker connection that dies mid-batch: every event in
    the batch must remain unpublished, never partially/falsely marked."""
    wf = _make_workflow(db)
    for i in range(3):
        persistence.create_task(db, workflow_id=wf["id"], name=f"t{i}", task_type="noop")

    relay = OutboxRelay(db, FakePublisher(raise_exc=ConnectionError("connection reset")))
    result = relay.run_once()

    assert result.published == 0
    assert result.errors == 3
    rows = _outbox_rows(db)
    assert all(r["published_at"] is None for r in rows)


# ---------------------------------------------------------------------------
# retry / duplicate publication
# ---------------------------------------------------------------------------


def test_unpublished_event_is_republished_on_retry(db):
    wf = _make_workflow(db)
    persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    failing = FakePublisher(confirm=False)
    OutboxRelay(db, failing).run_once()
    [event] = _outbox_rows(db)
    assert event["published_at"] is None

    succeeding = FakePublisher(confirm=True)
    result = OutboxRelay(db, succeeding).run_once()

    assert result.published == 1
    assert len(succeeding.calls) == 1
    [event] = _outbox_rows(db)
    assert event["published_at"] is not None


def test_already_published_event_is_not_reselected(db):
    wf = _make_workflow(db)
    persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    publisher = FakePublisher(confirm=True)
    relay = OutboxRelay(db, publisher)
    relay.run_once()
    assert len(publisher.calls) == 1

    second_result = relay.run_once()
    assert second_result.fetched == 0
    assert len(publisher.calls) == 1  # not republished


# ---------------------------------------------------------------------------
# ordering / batching
# ---------------------------------------------------------------------------


def test_events_processed_in_created_at_order(db):
    wf = _make_workflow(db)
    records = [
        persistence.create_task(db, workflow_id=wf["id"], name=f"t{i}", task_type="noop")
        for i in range(5)
    ]

    publisher = FakePublisher(confirm=True)
    relay = OutboxRelay(db, publisher)
    relay.run_once()

    task_ids_in_order = [payload["task_id"] for _, payload in publisher.decoded_calls()]
    assert task_ids_in_order == [r.id for r in records]


def test_batch_size_is_respected(db):
    wf = _make_workflow(db)
    for i in range(5):
        persistence.create_task(db, workflow_id=wf["id"], name=f"t{i}", task_type="noop")

    publisher = FakePublisher(confirm=True)
    relay = OutboxRelay(db, publisher, config=RelayConfig(batch_size=2))
    result = relay.run_once()

    assert result.fetched == 2
    assert result.published == 2

    second = relay.run_once()
    assert second.fetched == 2

    third = relay.run_once()
    assert third.fetched == 1


# ---------------------------------------------------------------------------
# database failure during marking
# ---------------------------------------------------------------------------


def test_db_failure_during_mark_published_does_not_report_success(db):
    wf = _make_workflow(db)
    persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    relay = OutboxRelay(db, FakePublisher(confirm=True))

    def _boom(event_id: int) -> bool:
        raise RuntimeError("db connection lost")

    relay._mark_published = _boom  # type: ignore[method-assign]
    result = relay.run_once()

    assert result.published == 0
    assert result.errors == 1
    [event] = _outbox_rows(db)
    assert event["published_at"] is None


# ---------------------------------------------------------------------------
# retry/backoff does not spin aggressively
# ---------------------------------------------------------------------------


def test_run_forever_backs_off_on_repeated_failure(db):
    wf = _make_workflow(db)
    persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    publisher = FakePublisher(raise_exc=ConnectionError("broker down"))
    relay = OutboxRelay(
        db,
        publisher,
        config=RelayConfig(
            batch_size=10,
            poll_interval_seconds=0.01,
            initial_backoff_seconds=0.05,
            max_backoff_seconds=0.2,
        ),
    )

    call_count = 0
    max_calls = 4

    def stop_check() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count > max_calls

    start = time.monotonic()
    relay.run_forever(stop_check=stop_check)
    elapsed = time.monotonic() - start

    # 4 backoff sleeps of 0.05, 0.1, 0.2, 0.2 = 0.55s minimum; comfortably
    # more than a tight loop (which would finish in microseconds) and
    # comfortably less than treating failures as a fixed long sleep.
    assert elapsed >= 0.3


def test_run_forever_resets_backoff_after_success(db):
    wf = _make_workflow(db)
    persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    publisher = FakePublisher(confirm=True)
    relay = OutboxRelay(
        db,
        publisher,
        config=RelayConfig(
            batch_size=10,
            poll_interval_seconds=0.01,
            initial_backoff_seconds=1.0,
            max_backoff_seconds=5.0,
        ),
    )

    call_count = 0

    def stop_check() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count > 2

    start = time.monotonic()
    relay.run_forever(stop_check=stop_check)
    elapsed = time.monotonic() - start

    # No errors ever occur, so only the short poll_interval is ever slept,
    # never the (much longer) initial/max backoff.
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# on_iteration (Phase 10 -- operational readiness)
# ---------------------------------------------------------------------------


def test_run_forever_calls_on_iteration_with_each_result(db):
    wf = _make_workflow(db)
    persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    publisher = FakePublisher(confirm=True)
    relay = OutboxRelay(
        db, publisher, config=RelayConfig(batch_size=10, poll_interval_seconds=0.01)
    )

    results = []
    call_count = 0

    def stop_check() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count > 2

    relay.run_forever(stop_check=stop_check, on_iteration=results.append)

    assert len(results) == 2
    assert all(isinstance(r, RelayIterationResult) for r in results)
    # First iteration actually published the seeded task's outbox event.
    assert results[0].published == 1


def test_run_forever_without_on_iteration_behaves_exactly_as_before(db):
    """Backward compatibility: omitting on_iteration entirely (the
    default, None) must not change anything about run_forever's existing
    behavior -- it simply isn't called."""
    wf = _make_workflow(db)
    persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    publisher = FakePublisher(confirm=True)
    relay = OutboxRelay(
        db, publisher, config=RelayConfig(batch_size=10, poll_interval_seconds=0.01)
    )

    call_count = 0

    def stop_check() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count > 2

    relay.run_forever(stop_check=stop_check)  # no on_iteration -- must not raise

    with db.connection() as conn:
        row = conn.execute(
            "SELECT published_at FROM outbox_events LIMIT 1"
        ).fetchone()
    assert row["published_at"] is not None


# ---------------------------------------------------------------------------
# outbox rows are never deleted, never given a new state column
# ---------------------------------------------------------------------------


def test_published_rows_are_not_deleted(db):
    wf = _make_workflow(db)
    persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    OutboxRelay(db, FakePublisher(confirm=True)).run_once()

    with db.connection() as conn:
        count = conn.execute("SELECT count(*) AS n FROM outbox_events").fetchone()["n"]
    assert count == 1
