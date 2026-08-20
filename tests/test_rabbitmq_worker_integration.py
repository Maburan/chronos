"""Real-RabbitMQ integration tests for Phase 4's consumer.

Deliberately separate from tests/test_mq_consumer.py and
tests/test_worker_task_worker.py, which run against fakes and are part of
the ordinary, always-run suite. Requires a real broker, addressed via
CHRONOS_TEST_RABBITMQ_URL (same gating convention as
tests/test_rabbitmq_integration.py for Phase 3's publisher). If that
variable isn't set, every test here is skipped cleanly.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

pika = pytest.importorskip("pika")

from mq.config import RabbitMQConfig
from mq.connection import RabbitMQConsumer, RabbitMQPublisher, connect
from mq.topology import declare_topology, queue_name_for_task_type

RABBITMQ_URL = os.environ.get("CHRONOS_TEST_RABBITMQ_URL")

pytestmark = pytest.mark.skipif(
    not RABBITMQ_URL,
    reason="CHRONOS_TEST_RABBITMQ_URL not set; skipping real-broker integration tests",
)


@pytest.fixture()
def config() -> RabbitMQConfig:
    suffix = uuid.uuid4().hex[:8]
    return RabbitMQConfig(
        url=RABBITMQ_URL,
        exchange=f"chronos.tasks.test.{suffix}",
        dead_letter_exchange=f"chronos.tasks.test.{suffix}.dlx",
        task_types=("integration_test_type",),
    )


def test_consumer_receives_a_published_message_with_manual_ack(config):
    pub_connection = connect(config)
    consume_connection = connect(config)
    try:
        channel = pub_connection.channel()
        declare_topology(channel, config)

        publisher = RabbitMQPublisher(pub_connection, config)
        body = json.dumps(
            {"event_id": 1, "task_id": 42, "workflow_id": 7, "task_type": "integration_test_type"}
        ).encode()
        assert publisher.publish(routing_key="task.integration_test_type", body=body) is True

        queue = queue_name_for_task_type("integration_test_type", exchange=config.exchange)
        consumer = RabbitMQConsumer(
            consume_connection, queue=queue, prefetch_count=1, inactivity_timeout=5.0
        )
        message = consumer.next_message()
        assert message is not None
        assert message.body == body

        message.ack()
    finally:
        pub_connection.close()
        consume_connection.close()


def test_unacked_message_is_redelivered_after_connection_drop(config):
    """Simulates worker-crash-after-claim-but-before-ack (crash scenario B
    from the Phase 4 plan): closing the consuming connection without
    acking must make the message available again to a fresh consumer."""
    pub_connection = connect(config)
    try:
        channel = pub_connection.channel()
        declare_topology(channel, config)

        publisher = RabbitMQPublisher(pub_connection, config)
        body = json.dumps(
            {"event_id": 2, "task_id": 43, "workflow_id": 7, "task_type": "integration_test_type"}
        ).encode()
        publisher.publish(routing_key="task.integration_test_type", body=body)

        queue = queue_name_for_task_type("integration_test_type", exchange=config.exchange)

        first_connection = connect(config)
        first_consumer = RabbitMQConsumer(
            first_connection, queue=queue, prefetch_count=1, inactivity_timeout=5.0
        )
        first_message = first_consumer.next_message()
        assert first_message is not None
        # Simulate a crash: drop the connection without acking.
        first_connection.close()

        second_connection = connect(config)
        try:
            second_consumer = RabbitMQConsumer(
                second_connection, queue=queue, prefetch_count=1, inactivity_timeout=10.0
            )
            redelivered = second_consumer.next_message()
            assert redelivered is not None
            assert redelivered.body == body
            redelivered.ack()
        finally:
            second_connection.close()
    finally:
        pub_connection.close()


def test_rejected_malformed_message_is_dead_lettered(config):
    pub_connection = connect(config)
    consume_connection = connect(config)
    try:
        channel = pub_connection.channel()
        declare_topology(channel, config)

        publisher = RabbitMQPublisher(pub_connection, config)
        malformed_body = b"not valid json"
        publisher.publish(routing_key="task.integration_test_type", body=malformed_body)

        queue = queue_name_for_task_type("integration_test_type", exchange=config.exchange)
        consumer = RabbitMQConsumer(
            consume_connection, queue=queue, prefetch_count=1, inactivity_timeout=5.0
        )
        message = consumer.next_message()
        assert message is not None
        message.reject(requeue=False)

        from mq.topology import dead_letter_queue_name_for_task_type

        dlq = dead_letter_queue_name_for_task_type(
            "integration_test_type", exchange=config.exchange
        )
        dlq_channel = pub_connection.channel()
        method_frame, _properties, dlq_body = dlq_channel.basic_get(queue=dlq, auto_ack=True)
        assert method_frame is not None
        assert dlq_body == malformed_body
    finally:
        pub_connection.close()
        consume_connection.close()
