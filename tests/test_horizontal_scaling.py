"""Horizontal scaling, part 2: multiple worker instances of the SAME task
type, consuming the SAME task-type queue, concurrently and safely.

These tests deliberately drive the EXISTING, unmodified machinery
(persistence.claim_task_by_id, expire_lease, report_task_*, the real
TaskWorker, the real Reaper) with several distinct worker ids standing in
for scaled replicas -- the only thing this phase changed is that those
distinct ids now come for free from the container hostname (see
tests/test_worker_identity.py). Nothing here weakens the PostgreSQL
source-of-truth model; PostgreSQL's conditional claim remains the sole
thing that makes concurrent consumption safe.

Split:

  * DB-backed (always run when Postgres is available, via the shared ``db``
    fixture): claim safety, concurrent processing, crash+reaper+re-claim
    across live replicas, retry, and the attempt-count concurrency control.
  * Broker-backed (skipped unless ``CHRONOS_TEST_RABBITMQ_URL`` is set):
    two real consumers sharing one real queue, and task-type isolation
    across queues -- the real Docker Compose / RabbitMQ path the brief
    asks for where the environment allows it.
"""

from __future__ import annotations

import os
import queue as queue_mod
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import pytest

from core.transitions import TaskState
from db import persistence
from db.connection import get_pool
from worker.executor import TaskExecutorRegistry
from worker.task_worker import TaskWorker

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _seed_ready_tasks(db, n: int, *, task_type: str = "noop", max_attempts: int = 1) -> list[int]:
    wf = persistence.create_workflow(db, name="hscale-wf", user_id=db.test_user_id)
    ids = []
    for i in range(n):
        record = persistence.create_task(
            db,
            workflow_id=wf["id"],
            name=f"t{i}",
            task_type=task_type,
            max_attempts=max_attempts,
        )
        ids.append(record.id)
    return ids


def _dispatch_body(task_id: int, *, task_type: str = "noop", workflow_id: int = 1) -> bytes:
    import json

    return json.dumps(
        {
            "event_id": task_id,
            "task_id": task_id,
            "workflow_id": workflow_id,
            "task_type": task_type,
        }
    ).encode("utf-8")


class _FakeMessage:
    def __init__(self, body: bytes):
        self.body = body

    def ack(self) -> None:
        pass

    def reject(self, *, requeue: bool) -> None:
        pass


class _SharedQueueSource:
    """A thread-safe stand-in for one RabbitMQ queue shared by N consumers:
    every message is handed to exactly one caller of ``next_message`` (like
    RabbitMQ's competing-consumer dispatch), and ``None`` once drained so a
    worker's poll loop exits. This is what lets several real ``TaskWorker``
    instances -- each with its OWN worker id and its OWN connection pool --
    compete for the SAME stream of dispatch hints in-process."""

    def __init__(self, bodies: list[bytes]):
        self._q: queue_mod.Queue = queue_mod.Queue()
        for body in bodies:
            self._q.put(body)

    def next_message(self):
        try:
            body = self._q.get_nowait()
        except queue_mod.Empty:
            return None
        return _FakeMessage(body)


# ===========================================================================
# 3 + (observable evidence). Multiple worker instances process multiple
#     ready tasks concurrently, and more than one replica demonstrably
#     participates.
# ===========================================================================


def test_scaled_replicas_process_ready_tasks_and_multiple_participate(db, db_dsn):
    n_tasks = 15
    n_replicas = 3
    task_ids = _seed_ready_tasks(db, n_tasks)

    source = _SharedQueueSource([_dispatch_body(tid) for tid in task_ids])

    # (task_id, worker_id) recorded from inside each execution -- the
    # observable evidence that a given task ran on a given replica.
    executed: list[tuple[int, str]] = []
    executed_lock = threading.Lock()

    def run_replica(worker_id: str):
        pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            def record(task_record, _wid=worker_id):
                # A small, deliberate delay so work genuinely overlaps
                # across replicas rather than one thread draining first.
                time.sleep(0.02)
                with executed_lock:
                    executed.append((task_record.id, _wid))

            registry = TaskExecutorRegistry({"noop": record})
            worker = TaskWorker(
                pool, source, registry, worker_id=worker_id, lease_duration=LEASE
            )
            # Drain: run_once() returns False only once the shared queue is
            # empty (no message this poll).
            while worker.run_once():
                pass
        finally:
            pool.close()

    worker_ids = [f"worker-noop-replica-{i}" for i in range(n_replicas)]
    with ThreadPoolExecutor(max_workers=n_replicas) as executor:
        futures = [executor.submit(run_replica, wid) for wid in worker_ids]
        for future in as_completed(futures):
            future.result()  # re-raise anything

    executed_task_ids = [tid for tid, _ in executed]
    participating_workers = {wid for _, wid in executed}

    # every task ran exactly once...
    assert sorted(executed_task_ids) == sorted(task_ids)
    assert len(executed_task_ids) == len(set(executed_task_ids))
    # ...and this was genuine horizontal participation, not one worker doing
    # everything while the others idled.
    assert len(participating_workers) >= 2

    # PostgreSQL agrees: all terminal-succeeded, no leftover leases.
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT state, lease_owner FROM tasks WHERE id = ANY(%s)", (task_ids,)
        ).fetchall()
    assert all(r["state"] == "SUCCEEDED" for r in rows)
    assert all(r["lease_owner"] is None for r in rows)


# ===========================================================================
# 4. The same task cannot be successfully claimed/executed by two workers,
#    even when both replicas receive a dispatch hint for it (a redelivery /
#    duplicate on the shared queue).
# ===========================================================================


def test_same_task_hint_delivered_to_two_replicas_executes_once(db, db_dsn):
    [task_id] = _seed_ready_tasks(db, 1)

    executed: list[str] = []
    executed_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def run_replica(worker_id: str):
        pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            def record(_task_record, _wid=worker_id):
                with executed_lock:
                    executed.append(_wid)

            registry = TaskExecutorRegistry({"noop": record})
            # Each replica gets its OWN copy of the hint for the SAME task.
            source = _SharedQueueSource([_dispatch_body(task_id)])
            worker = TaskWorker(
                pool, source, registry, worker_id=worker_id, lease_duration=LEASE
            )
            barrier.wait()  # both fire their claim attempt at ~the same instant
            worker.run_once()
        finally:
            pool.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(run_replica, "worker-noop-a"),
            executor.submit(run_replica, "worker-noop-b"),
        ]
        for future in as_completed(futures):
            future.result()

    # Exactly one replica actually executed the task; the other's claim
    # returned None (already RUNNING) and it did nothing.
    assert len(executed) == 1

    with db.connection() as conn:
        row = conn.execute(
            "SELECT state, attempt_count FROM tasks WHERE id = %s", (task_id,)
        ).fetchone()
    assert row["state"] == "SUCCEEDED"
    assert row["attempt_count"] == 1  # never double-claimed / double-incremented


# ===========================================================================
# 5 + 6 + 7. Worker A crashes mid-task; the Reaper recovers it; worker B
#            (a different live replica) claims and completes it -- with a
#            third replica also alive throughout.
# ===========================================================================


def test_crash_then_reaper_then_other_replica_completes(db, db_dsn):
    from reaper.reaper import Reaper

    [task_id] = _seed_ready_tasks(db, 1, max_attempts=2)

    worker_a = "worker-noop-a"
    worker_b = "worker-noop-b"
    worker_c = "worker-noop-c"

    # -- worker A claims (RUNNING under A's lease) then "crashes": it simply
    #    never reports. Its lease is what the reaper will later reclaim.
    short_lease = timedelta(seconds=30)
    claimed_a = persistence.claim_task_by_id(
        db, task_id=task_id, worker_id=worker_a, now=NOW, lease_duration=short_lease
    )
    assert claimed_a is not None
    assert claimed_a.instance.lease_owner == worker_a
    assert claimed_a.instance.attempt_count == 1

    # -- while A holds the lease, another live replica (C) that happens to
    #    see a stale/duplicate hint for the same task cannot steal it.
    stolen = persistence.claim_task_by_id(
        db, task_id=task_id, worker_id=worker_c, now=NOW, lease_duration=LEASE
    )
    assert stolen is None

    # -- the lease expires; the real Reaper reclaims A's dead task.
    after_expiry = NOW + short_lease + timedelta(seconds=1)
    reaper = Reaper(db, clock=lambda: after_expiry)
    result = reaper.run_once()
    assert result.candidates == 1
    assert result.reclaimed == 1
    assert result.lost_races == 0

    reclaimed = persistence.get_task(db, task_id=task_id)
    assert reclaimed.instance.state.value == "READY"
    assert reclaimed.instance.lease_owner is None
    assert reclaimed.instance.attempt_count == 1  # 1 < 2, retry available

    # -- worker B, a different live replica, now claims and completes it.
    claimed_b = persistence.claim_task_by_id(
        db, task_id=task_id, worker_id=worker_b, now=after_expiry, lease_duration=LEASE
    )
    assert claimed_b is not None
    assert claimed_b.instance.lease_owner == worker_b
    assert claimed_b.instance.attempt_count == 2

    report = persistence.report_task_success(
        db, task_id=task_id, worker_id=worker_b, now=after_expiry
    )
    assert report.applied is True

    # -- A's zombie success report (arriving late, after B already owns and
    #    finished it) is fenced out: B's result stands.
    stale = persistence.report_task_success(
        db, task_id=task_id, worker_id=worker_a, now=after_expiry
    )
    assert stale.applied is False

    final = persistence.get_task(db, task_id=task_id)
    assert final.instance.state.value == "SUCCEEDED"
    assert final.instance.attempt_count == 2
    assert final.instance.lease_owner is None


# ===========================================================================
# 8. Existing retry behavior still works (unchanged by scaling), across two
#    different replicas handling the two attempts.
# ===========================================================================


def test_retry_across_replicas_reaches_terminal_failure(db, db_dsn):
    [task_id] = _seed_ready_tasks(db, 1, max_attempts=2)

    # attempt 1 on replica A -> failure -> READY again (1 < 2)
    claimed_a = persistence.claim_task_by_id(
        db, task_id=task_id, worker_id="worker-noop-a", now=NOW, lease_duration=LEASE
    )
    assert claimed_a.instance.attempt_count == 1
    r1 = persistence.report_task_failure(
        db, task_id=task_id, worker_id="worker-noop-a", now=NOW
    )
    assert r1.applied is True
    assert persistence.get_task(db, task_id=task_id).instance.state == TaskState.READY

    # attempt 2 on replica B -> failure -> FAILED_TERMINAL (2 == max)
    claimed_b = persistence.claim_task_by_id(
        db, task_id=task_id, worker_id="worker-noop-b", now=NOW, lease_duration=LEASE
    )
    assert claimed_b.instance.attempt_count == 2
    r2 = persistence.report_task_failure(
        db, task_id=task_id, worker_id="worker-noop-b", now=NOW
    )
    assert r2.applied is True

    final = persistence.get_task(db, task_id=task_id)
    assert final.instance.state == TaskState.FAILED_TERMINAL
    assert final.instance.attempt_count == 2


# ===========================================================================
# 9. Existing concurrency control still holds: attempt_count never exceeds
#    max_attempts even when many replicas hammer the SAME task via the
#    targeted claim (the RabbitMQ dispatch path).
# ===========================================================================


def test_attempt_count_never_exceeds_max_across_replicas(db, db_dsn):
    max_attempts = 3
    [task_id] = _seed_ready_tasks(db, 1, max_attempts=max_attempts)

    def hammer(worker_id: str):
        pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            for _ in range(max_attempts + 5):
                claimed = persistence.claim_task_by_id(
                    pool,
                    task_id=task_id,
                    worker_id=worker_id,
                    now=NOW,
                    lease_duration=LEASE,
                )
                if claimed is None:
                    continue
                persistence.report_task_failure(
                    pool, task_id=task_id, worker_id=worker_id, now=NOW
                )
        finally:
            pool.close()

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(hammer, f"worker-noop-{i}") for i in range(6)]
        for future in as_completed(futures):
            future.result()

    with db.connection() as conn:
        row = conn.execute(
            "SELECT state, attempt_count, max_attempts FROM tasks WHERE id = %s",
            (task_id,),
        ).fetchone()
    assert row["state"] == "FAILED_TERMINAL"
    assert row["attempt_count"] == max_attempts
    assert row["attempt_count"] <= row["max_attempts"]


# ===========================================================================
# Broker-backed tests: the real RabbitMQ path (skipped without a broker).
# ===========================================================================

RABBITMQ_URL = os.environ.get("CHRONOS_TEST_RABBITMQ_URL")

_broker = pytest.mark.skipif(
    not RABBITMQ_URL,
    reason="CHRONOS_TEST_RABBITMQ_URL not set; skipping real-RabbitMQ scaling tests",
)


@pytest.fixture()
def rabbitmq_config():
    pytest.importorskip("pika")
    from mq.config import RabbitMQConfig

    suffix = uuid.uuid4().hex[:8]
    return RabbitMQConfig(
        url=RABBITMQ_URL,
        exchange=f"chronos.tasks.hscale.{suffix}",
        dead_letter_exchange=f"chronos.tasks.hscale.{suffix}.dlx",
        task_types=("echo", "sleep"),
    )


# ---------------------------------------------------------------------------
# 1. Two worker instances can consume from the SAME task-type queue.
# ---------------------------------------------------------------------------


@_broker
def test_two_consumers_share_one_queue(rabbitmq_config):
    from mq.connection import RabbitMQConsumer, RabbitMQPublisher, connect
    from mq.topology import declare_topology, queue_name_for_task_type, routing_key_for_task_type

    declare_conn = connect(rabbitmq_config)
    declare_topology(declare_conn.channel(), rabbitmq_config)

    publish_conn = connect(rabbitmq_config)
    publisher = RabbitMQPublisher(publish_conn, rabbitmq_config)

    queue = queue_name_for_task_type("echo", exchange=rabbitmq_config.exchange)
    routing_key = routing_key_for_task_type("echo")

    conn_1 = connect(rabbitmq_config)
    conn_2 = connect(rabbitmq_config)
    consumer_1 = RabbitMQConsumer(conn_1, queue=queue, prefetch_count=1, inactivity_timeout=5.0)
    consumer_2 = RabbitMQConsumer(conn_2, queue=queue, prefetch_count=1, inactivity_timeout=5.0)

    try:
        # Two dispatch hints onto the ONE shared echo queue.
        for tid in (101, 102):
            publisher.publish(routing_key=routing_key, body=_dispatch_body(tid, task_type="echo"))

        # Each consumer takes one -- RabbitMQ's competing-consumer dispatch
        # across two instances on the same queue.
        m1 = consumer_1.next_message()
        m2 = consumer_2.next_message()
        assert m1 is not None
        assert m2 is not None
        m1.ack()
        m2.ack()

        bodies = {m1.body, m2.body}
        assert bodies == {
            _dispatch_body(101, task_type="echo"),
            _dispatch_body(102, task_type="echo"),
        }
    finally:
        consumer_1.close()
        consumer_2.close()
        publisher.close()
        declare_conn.close()


# ---------------------------------------------------------------------------
# 10. Worker/task-type isolation still holds: a message for one task type
#     is never delivered to a consumer on a different task type's queue,
#     even with multiple queues declared on one exchange.
# ---------------------------------------------------------------------------


@_broker
def test_task_type_queue_isolation(rabbitmq_config):
    from mq.connection import RabbitMQConsumer, RabbitMQPublisher, connect
    from mq.topology import declare_topology, queue_name_for_task_type, routing_key_for_task_type

    declare_conn = connect(rabbitmq_config)
    declare_topology(declare_conn.channel(), rabbitmq_config)

    publish_conn = connect(rabbitmq_config)
    publisher = RabbitMQPublisher(publish_conn, rabbitmq_config)

    echo_queue = queue_name_for_task_type("echo", exchange=rabbitmq_config.exchange)
    sleep_queue = queue_name_for_task_type("sleep", exchange=rabbitmq_config.exchange)

    echo_consumer = RabbitMQConsumer(
        connect(rabbitmq_config), queue=echo_queue, prefetch_count=1, inactivity_timeout=3.0
    )
    sleep_consumer = RabbitMQConsumer(
        connect(rabbitmq_config), queue=sleep_queue, prefetch_count=1, inactivity_timeout=3.0
    )

    try:
        # Publish ONLY an echo hint.
        publisher.publish(
            routing_key=routing_key_for_task_type("echo"),
            body=_dispatch_body(201, task_type="echo"),
        )

        # The sleep consumer must see nothing (isolation); the echo
        # consumer gets the message.
        assert sleep_consumer.next_message() is None
        echo_msg = echo_consumer.next_message()
        assert echo_msg is not None
        echo_msg.ack()
        assert echo_msg.body == _dispatch_body(201, task_type="echo")
    finally:
        echo_consumer.close()
        sleep_consumer.close()
        publisher.close()
        declare_conn.close()
