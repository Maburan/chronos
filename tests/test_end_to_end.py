"""Phase 9: end-to-end tests -- the complete Chronos lifecycle through
real PostgreSQL and real RabbitMQ, driven entirely through the actual
HTTP API and the actual orchestration components (OutboxRelay,
RabbitMQConsumer, TaskWorker). Nothing here is mocked or faked.

Deliberately separate from every other test file: this is the only place
that wires the relay and worker together against a real broker in the
same test, proving the full chain rather than any single hop of it.
Gated on CHRONOS_TEST_RABBITMQ_URL, same convention as every other
real-broker test file (e.g. tests/test_rabbitmq_worker_integration.py).

Deterministic and bounded throughout: every relay/worker step is a single
run_once() call, never run_forever() -- no background threads, no
polling loops, no timing races to manage.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest

pika = pytest.importorskip("pika")

from mq.config import RabbitMQConfig, RelayConfig
from mq.connection import RabbitMQConsumer, RabbitMQPublisher, connect
from mq.topology import declare_task_type, declare_topology, queue_name_for_task_type
from relay.outbox_relay import OutboxRelay
from worker.demo_task import ECHO_TASK_TYPE, echo_task
from worker.executor import TaskExecutorRegistry
from worker.task_worker import TaskWorker

RABBITMQ_URL = os.environ.get("CHRONOS_TEST_RABBITMQ_URL")

pytestmark = pytest.mark.skipif(
    not RABBITMQ_URL,
    reason="CHRONOS_TEST_RABBITMQ_URL not set; skipping end-to-end tests",
)


@pytest.fixture()
def rabbitmq_config() -> RabbitMQConfig:
    # A unique exchange/DLX per test avoids collisions against a shared
    # broker, matching tests/test_rabbitmq_integration.py's convention.
    suffix = uuid.uuid4().hex[:8]
    return RabbitMQConfig(
        url=RABBITMQ_URL,
        exchange=f"chronos.tasks.e2e.{suffix}",
        dead_letter_exchange=f"chronos.tasks.e2e.{suffix}.dlx",
        task_types=(ECHO_TASK_TYPE,),
    )


@pytest.fixture()
def e2e_stack(db, rabbitmq_config):
    """A real relay and a real worker, both wired to the same real
    broker and the same real database pool (``db`` -- shared with the
    ``api_client`` fixture in the same test, so every hop sees the exact
    same committed state). Exposed as objects so tests can call
    ``run_once()`` on each explicitly, one step at a time."""
    publish_connection = connect(rabbitmq_config)
    channel = publish_connection.channel()
    declare_topology(channel, rabbitmq_config)
    declare_task_type(channel, ECHO_TASK_TYPE, rabbitmq_config)

    publisher = RabbitMQPublisher(publish_connection, rabbitmq_config)
    relay = OutboxRelay(db, publisher, config=RelayConfig())

    consume_connection = connect(rabbitmq_config)
    queue = queue_name_for_task_type(ECHO_TASK_TYPE, exchange=rabbitmq_config.exchange)
    consumer = RabbitMQConsumer(
        consume_connection, queue=queue, prefetch_count=10, inactivity_timeout=5.0
    )

    registry = TaskExecutorRegistry({ECHO_TASK_TYPE: echo_task})
    task_worker = TaskWorker(
        db,
        consumer,
        registry,
        worker_id="e2e-worker",
        lease_duration=timedelta(minutes=5),
    )

    try:
        yield relay, task_worker
    finally:
        publisher.close()  # closes both its channel and connection
        consumer.close()  # closes both its channel and connection


def test_full_lifecycle_via_http_and_real_broker(api_client, e2e_stack):
    """API create -> READY -> outbox -> real RabbitMQ -> real consumer ->
    targeted claim -> echo_task executes -> report_task_success ->
    SUCCEEDED, visible through the actual GET API."""
    relay, task_worker = e2e_stack

    workflow = api_client.post("/workflows", json={"name": "e2e-happy-path"}).json()
    task = api_client.post(
        f"/workflows/{workflow['id']}/tasks",
        json={"name": "t1", "task_type": ECHO_TASK_TYPE, "payload": {"message": "hello"}},
    ).json()
    assert task["state"] == "READY"

    relay_result = relay.run_once()
    assert relay_result.published == 1

    handled = task_worker.run_once()
    assert handled is True

    final = api_client.get(f"/tasks/{task['id']}").json()
    assert final["state"] == "SUCCEEDED"
    assert final["attempt_count"] == 1
    assert final["lease_owner"] is None


def test_reported_failure_retries_then_succeeds(api_client, e2e_stack):
    """A task that deterministically fails its first attempt (via
    echo_task's fail_until_attempt payload knob) must retry through the
    real outbox/broker/worker chain a second time and succeed, with
    attempt_count reflecting both attempts -- all visible through the
    actual GET API at each step."""
    relay, task_worker = e2e_stack

    workflow = api_client.post("/workflows", json={"name": "e2e-retry"}).json()
    task = api_client.post(
        f"/workflows/{workflow['id']}/tasks",
        json={
            "name": "t1",
            "task_type": ECHO_TASK_TYPE,
            "max_attempts": 2,
            "payload": {"message": "retry-me", "fail_until_attempt": 2},
        },
    ).json()
    assert task["state"] == "READY"

    # First dispatch + attempt: echo_task raises (attempt_count will be 1,
    # which is < fail_until_attempt=2), report_task_failure retries since
    # attempts remain.
    first_relay_result = relay.run_once()
    assert first_relay_result.published == 1
    first_handled = task_worker.run_once()
    assert first_handled is True

    mid = api_client.get(f"/tasks/{task['id']}").json()
    assert mid["state"] == "READY"  # retried, not terminal
    assert mid["attempt_count"] == 1

    # The retry itself produced a new outbox event -- second dispatch +
    # attempt: attempt_count becomes 2, which meets fail_until_attempt, so
    # echo_task succeeds this time.
    second_relay_result = relay.run_once()
    assert second_relay_result.published == 1
    second_handled = task_worker.run_once()
    assert second_handled is True

    final = api_client.get(f"/tasks/{task['id']}").json()
    assert final["state"] == "SUCCEEDED"
    assert final["attempt_count"] == 2
