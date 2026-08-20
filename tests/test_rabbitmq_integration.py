"""Real-RabbitMQ integration tests for Phase 3.

Deliberately separate from ``tests/test_rabbitmq_topology.py`` and
``tests/test_outbox_relay.py``, which run against a fake channel/publisher
and are part of the ordinary, always-run suite (per the handoff's
requirement that the standard test run never require a developer to
operate RabbitMQ manually).

This file requires a real broker, addressed via
``CHRONOS_TEST_RABBITMQ_URL`` (mirroring how ``tests/conftest.py`` resolves
``CHRONOS_TEST_DATABASE_URL`` for Postgres). If that variable isn't set,
every test here is skipped cleanly -- this file is never a reason the rest
of the suite fails or hangs.
"""

from __future__ import annotations

import os
import uuid

import pytest

pika = pytest.importorskip("pika")

from mq.config import RabbitMQConfig
from mq.connection import RabbitMQPublisher, connect
from mq.topology import declare_topology, queue_name_for_task_type

RABBITMQ_URL = os.environ.get("CHRONOS_TEST_RABBITMQ_URL")

pytestmark = pytest.mark.skipif(
    not RABBITMQ_URL,
    reason="CHRONOS_TEST_RABBITMQ_URL not set; skipping real-broker integration tests",
)


@pytest.fixture()
def config() -> RabbitMQConfig:
    # A unique exchange/DLX per test run avoids collisions between runs
    # against a shared broker, without needing any broker-side cleanup
    # logic beyond what a real deployment would already have.
    suffix = uuid.uuid4().hex[:8]
    return RabbitMQConfig(
        url=RABBITMQ_URL,
        exchange=f"chronos.tasks.test.{suffix}",
        dead_letter_exchange=f"chronos.tasks.test.{suffix}.dlx",
        task_types=("integration_test_type",),
    )


def test_topology_declares_against_real_broker(config):
    connection = connect(config)
    try:
        channel = connection.channel()
        declare_topology(channel, config)
        # Redeclaring must not raise (idempotent against a real broker).
        declare_topology(channel, config)

        queue = queue_name_for_task_type("integration_test_type", exchange=config.exchange)
        method = channel.queue_declare(queue=queue, durable=True, passive=True)
        assert method.method.queue == queue
    finally:
        connection.close()


def test_publish_with_confirm_against_real_broker(config):
    connection = connect(config)
    try:
        channel = connection.channel()
        declare_topology(channel, config)

        publisher = RabbitMQPublisher(connection, config)
        confirmed = publisher.publish(
            routing_key="task.integration_test_type", body=b'{"hello": "world"}'
        )
        assert confirmed is True

        queue = queue_name_for_task_type("integration_test_type", exchange=config.exchange)
        method_frame, _, body = channel.basic_get(queue=queue, auto_ack=True)
        assert method_frame is not None
        assert body == b'{"hello": "world"}'
    finally:
        connection.close()
