"""Phase 11: proves the existing crash-recovery path end-to-end against a
real PostgreSQL, a real RabbitMQ, and a genuinely separate worker OS
process -- not a mock, not a fake, not a direct row mutation standing in
for "the worker died."

    real worker subprocess claims task_id=N (RUNNING, short lease)
        |
        | proc.kill()  <-- a hard, immediate process termination; the
        v                  worker's heartbeat thread (a daemon thread in
    [process gone]          that same process) dies with it -- there is
                             no separate mechanism to stop, and nothing to
                             fake, because Chronos has never had any other
                             way for a worker to "still be alive" than
                             "the OS process is still running."
        |
        | (task remains RUNNING in Postgres; lease still valid for a bit)
        v
    lease expires -- real Reaper.run_once() reclaims it (existing,
    unmodified reaper/reaper.py + db/persistence.py.expire_lease)
        |
        | READY, exactly one new outbox event (retry path)
        v
    real OutboxRelay.run_once() republishes it to real RabbitMQ
        |
        v
    a second real worker (in-process TaskWorker + real RabbitMQConsumer)
    consumes the redelivery, claims, executes, reports -> SUCCEEDED

Nothing here redesigns claim_task_by_id, expire_lease, the outbox, the
relay, or worker semantics -- this test only *drives* those existing,
unmodified pieces through a genuine crash and observes the existing
recovery behavior they already implement (see reaper/reaper.py's module
docstring, worker/task_worker.py's module docstring, and
tests/test_reaper.py, all re-read before writing this test).

One deliberate terminology note versus a literal reading of "crash after
claim but before ACK": in the existing, unmodified worker design, the
RabbitMQ ACK happens immediately once the claim attempt resolves and
*before* execution starts (see worker/task_worker.py::_handle_message and
its module docstring: "RabbitMQ's job ends at 'was this dispatch hint
delivered to a consumer,' not 'did the task finish.'"). The ACK is
explicitly non-authoritative and plays no part in crash recovery -- only
the task's row in Postgres (state, lease_owner, lease_expires_at) does.
So "crash before ACK" and "crash before completion" are not
distinguishable in any way that matters to recovery correctness in this
architecture, and reproducing the literal pre-ACK microsecond window
would not exercise anything the reaper/outbox/relay path doesn't already
cover by "crash before completion." This test crashes the worker while
it is genuinely blocked inside task execution (after claim, after ACK,
long before report_task_success/failure) -- the reachable, meaningful
window for this design.

Deliberately real-time, not a synced/fake clock: this test is already
seconds-scale (a real subprocess, a real short lease, a real kill), so it
simply waits out the lease in wall-clock time rather than adopting
tests/test_heartbeat_concurrency.py's ``_synced_clock`` trick, which
exists for tests that need sub-second precision against fixed test-epoch
data. Not flaky: every wait here is a bounded poll with a generous
timeout, never a fixed sleep sized to "probably long enough."
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pika = pytest.importorskip("pika")

from db import persistence
from mq.config import RabbitMQConfig, RelayConfig
from mq.connection import RabbitMQConsumer, RabbitMQPublisher, connect
from mq.topology import declare_task_type, declare_topology, queue_name_for_task_type
from reaper.reaper import Reaper
from relay.outbox_relay import OutboxRelay
from worker.demo_task import ECHO_TASK_TYPE, echo_task
from worker.executor import TaskExecutorRegistry
from worker.task_worker import TaskWorker

RABBITMQ_URL = os.environ.get("CHRONOS_TEST_RABBITMQ_URL")

pytestmark = pytest.mark.skipif(
    not RABBITMQ_URL,
    reason="CHRONOS_TEST_RABBITMQ_URL not set; skipping crash-recovery test",
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Long enough that a subprocess spawn + broker connect + claim reliably
# happens well before it elapses (observed comfortably under 2s locally);
# short enough not to make the suite painfully slow.
CRASH_WORKER_LEASE_SECONDS = 3.0
# Longer than the lease, so the task is still genuinely executing (and
# therefore still RUNNING) at the moment we kill the process -- if the
# sleep were shorter than the lease, echo_task could finish and report
# success before the kill ever lands.
TASK_SLEEP_SECONDS = 8.0
CLAIM_POLL_TIMEOUT_SECONDS = 15.0
LEASE_EXPIRY_POLL_TIMEOUT_SECONDS = CRASH_WORKER_LEASE_SECONDS + 10.0


def _outbox_count(pool) -> int:
    with pool.connection() as conn:
        return conn.execute("SELECT count(*) AS n FROM outbox_events").fetchone()["n"]


def _unpublished_outbox_count(pool) -> int:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM outbox_events WHERE published_at IS NULL"
        ).fetchone()["n"]


def _wait_until(predicate, *, timeout_seconds: float, interval_seconds: float = 0.1, description: str):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value is not None:
            return value
        time.sleep(interval_seconds)
    pytest.fail(f"timed out after {timeout_seconds}s waiting for: {description}")


@pytest.fixture()
def rabbitmq_config() -> RabbitMQConfig:
    # A unique exchange/DLX per test avoids collisions against a shared
    # broker -- same convention as tests/test_end_to_end.py.
    suffix = uuid.uuid4().hex[:8]
    return RabbitMQConfig(
        url=RABBITMQ_URL,
        exchange=f"chronos.tasks.crash.{suffix}",
        dead_letter_exchange=f"chronos.tasks.crash.{suffix}.dlx",
        task_types=(ECHO_TASK_TYPE,),
    )


def test_worker_crash_after_claim_is_recovered_via_reaper_and_redelivery(
    db, db_dsn, api_client, rabbitmq_config
):
    # -- setup: real topology, a real task via the real HTTP API ---------
    declare_connection = connect(rabbitmq_config)
    declare_channel = declare_connection.channel()
    declare_topology(declare_channel, rabbitmq_config)
    declare_task_type(declare_channel, ECHO_TASK_TYPE, rabbitmq_config)

    publish_connection = connect(rabbitmq_config)
    publisher = RabbitMQPublisher(publish_connection, rabbitmq_config)
    relay = OutboxRelay(db, publisher, config=RelayConfig())

    worker_proc: subprocess.Popen | None = None
    second_worker_connection = None
    second_consumer = None

    try:
        workflow = api_client.post("/workflows", json={"name": "crash-recovery"}).json()
        task = api_client.post(
            f"/workflows/{workflow['id']}/tasks",
            json={
                "name": "t1",
                "task_type": ECHO_TASK_TYPE,
                "max_attempts": 2,
                "payload": {
                    "message": "crash-test",
                    # Only the first (pre-crash) attempt sleeps; the
                    # redelivered second attempt completes immediately.
                    "sleep_seconds": TASK_SLEEP_SECONDS,
                    "sleep_until_attempt": 2,
                },
            },
        ).json()
        task_id = task["id"]
        assert task["state"] == "READY"

        before_outbox = _outbox_count(db)  # 1, from creation
        assert _unpublished_outbox_count(db) == 1

        relay_result = relay.run_once()
        assert relay_result.published == 1
        assert _unpublished_outbox_count(db) == 0

        # -- crash simulation: a real, separate OS process --------------
        env = dict(os.environ)
        env.update(
            {
                "CHRONOS_DATABASE_URL": db_dsn,
                "CHRONOS_RABBITMQ_URL": rabbitmq_config.url,
                "CHRONOS_RABBITMQ_EXCHANGE": rabbitmq_config.exchange,
                "CHRONOS_RABBITMQ_DLX": rabbitmq_config.dead_letter_exchange,
                "CHRONOS_TASK_TYPES": ECHO_TASK_TYPE,
                "CHRONOS_WORKER_ID": "crash-worker",
                "CHRONOS_WORKER_TASK_TYPE": ECHO_TASK_TYPE,
                "CHRONOS_WORKER_LEASE_SECONDS": str(CRASH_WORKER_LEASE_SECONDS),
                "CHRONOS_WORKER_PREFETCH": "1",
                "CHRONOS_WORKER_POLL_IDLE_SLEEP_SECONDS": "0.1",
            }
        )
        worker_proc = subprocess.Popen(
            [sys.executable, "-m", "worker"],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        def _check_running():
            record = persistence.get_task(db, task_id=task_id)
            if record is not None and record.instance.state.value == "RUNNING":
                return record
            return None

        claimed_record = _wait_until(
            _check_running,
            timeout_seconds=CLAIM_POLL_TIMEOUT_SECONDS,
            description="crash-worker subprocess to claim the task (state == RUNNING)",
        )
        assert claimed_record.instance.lease_owner == "crash-worker"
        assert claimed_record.instance.attempt_count == 1
        lease_expires_at = claimed_record.instance.lease_expires_at
        assert lease_expires_at is not None

        # -- the crash itself: a hard kill, no graceful shutdown --------
        # Deliberately NOT SIGINT/SIGTERM: Phase 10 already established
        # that graceful signal delivery is unreliable on Windows, and a
        # graceful shutdown is the wrong thing to simulate here anyway --
        # a real crash gives no chance to run cleanup handlers.
        # Popen.kill() -> TerminateProcess on Windows, an immediate, hard
        # termination; the worker's heartbeat thread (a daemon thread in
        # the same process) has no way to survive it or to run one more
        # renew_lease call.
        worker_proc.kill()
        worker_proc.wait(timeout=10)
        assert worker_proc.poll() is not None  # the process is genuinely gone

        # -- task remains RUNNING while the lease is still valid --------
        still_running = persistence.get_task(db, task_id=task_id)
        assert still_running.instance.state.value == "RUNNING"
        assert still_running.instance.lease_owner == "crash-worker"
        assert still_running.instance.attempt_count == 1

        # -- wait out the lease, for real -------------------------------
        def _lease_expired():
            return True if datetime.now(timezone.utc) > lease_expires_at else None

        _wait_until(
            _lease_expired,
            timeout_seconds=LEASE_EXPIRY_POLL_TIMEOUT_SECONDS,
            interval_seconds=0.2,
            description="the crashed worker's lease to actually expire",
        )

        # -- real Reaper, real PostgreSQL --------------------------------
        reaper = Reaper(db)
        reaper_result = reaper.run_once()
        assert reaper_result.candidates == 1
        assert reaper_result.reclaimed == 1
        assert reaper_result.lost_races == 0
        assert reaper_result.errors == 0

        reclaimed = persistence.get_task(db, task_id=task_id)
        assert reclaimed.instance.state.value == "READY"  # attempts remain (1 < 2)
        assert reclaimed.instance.lease_owner is None
        assert reclaimed.instance.lease_expires_at is None
        assert reclaimed.instance.attempt_count == 1  # unchanged by expire_lease itself

        # -- exactly one new outbox event from the reclaim --------------
        after_reclaim_outbox = _outbox_count(db)
        assert after_reclaim_outbox == before_outbox + 1
        assert _unpublished_outbox_count(db) == 1

        # -- real OutboxRelay republishes the retry -----------------------
        second_relay_result = relay.run_once()
        assert second_relay_result.published == 1
        assert _unpublished_outbox_count(db) == 0

        # -- a second real worker consumes the redelivery ----------------
        second_worker_connection = connect(rabbitmq_config)
        queue = queue_name_for_task_type(ECHO_TASK_TYPE, exchange=rabbitmq_config.exchange)
        second_consumer = RabbitMQConsumer(
            second_worker_connection, queue=queue, prefetch_count=10, inactivity_timeout=10.0
        )
        registry = TaskExecutorRegistry({ECHO_TASK_TYPE: echo_task})
        second_task_worker = TaskWorker(
            db,
            second_consumer,
            registry,
            worker_id="second-worker",
            lease_duration=timedelta(minutes=5),
        )
        handled = second_task_worker.run_once()
        assert handled is True

        # -- final state, verified through the real HTTP API -------------
        final = api_client.get(f"/tasks/{task_id}").json()
        assert final["state"] == "SUCCEEDED"
        assert final["attempt_count"] == 2
        assert final["lease_owner"] is None
        assert final["lease_expires_at"] is None

        # -- invariants: no extra outbox events, nothing left unpublished
        assert _outbox_count(db) == before_outbox + 1  # creation + one reclaim, no more
        assert _unpublished_outbox_count(db) == 0

    finally:
        if worker_proc is not None and worker_proc.poll() is None:
            worker_proc.kill()
            worker_proc.wait(timeout=10)
        if second_consumer is not None:
            second_consumer.close()
        if second_worker_connection is not None and second_consumer is None:
            second_worker_connection.close()
        publisher.close()
        declare_connection.close()
