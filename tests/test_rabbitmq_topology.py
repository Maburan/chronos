"""Phase 3 topology tests.

These exercise ``mq/topology.py`` against a lightweight in-memory fake
channel -- never a real broker -- so they run deterministically as part of
the ordinary suite. Real-broker verification belongs in
``tests/test_rabbitmq_integration.py`` (separately gated, see that file).
"""

from __future__ import annotations

import pytest

from mq.config import RabbitMQConfig
from mq.topology import (
    dead_letter_queue_name_for_task_type,
    declare_task_type,
    declare_topology,
    queue_name_for_task_type,
    routing_key_for_task_type,
)


class FakeChannel:
    """Records every declare/bind call and mimics just enough of AMQP
    semantics to make idempotency meaningful: redeclaring an
    exchange/queue with the exact same arguments is a silent no-op (as it
    would be against a real broker); redeclaring with *different*
    arguments raises, matching RabbitMQ's own PRECONDITION_FAILED
    behavior."""

    def __init__(self) -> None:
        self.exchanges: dict[str, dict] = {}
        self.queues: dict[str, dict] = {}
        self.bindings: set[tuple[str, str, str]] = set()
        self.exchange_declare_calls = 0
        self.queue_declare_calls = 0
        self.queue_bind_calls = 0

    def exchange_declare(self, exchange: str, exchange_type: str, durable: bool) -> None:
        self.exchange_declare_calls += 1
        params = {"type": exchange_type, "durable": durable}
        existing = self.exchanges.get(exchange)
        if existing is not None and existing != params:
            raise ValueError(f"PRECONDITION_FAILED: {exchange} redeclared with different args")
        self.exchanges[exchange] = params

    def queue_declare(self, queue: str, durable: bool, arguments: dict) -> None:
        self.queue_declare_calls += 1
        params = {"durable": durable, "arguments": arguments}
        existing = self.queues.get(queue)
        if existing is not None and existing != params:
            raise ValueError(f"PRECONDITION_FAILED: {queue} redeclared with different args")
        self.queues[queue] = params

    def queue_bind(self, queue: str, exchange: str, routing_key: str) -> None:
        self.queue_bind_calls += 1
        self.bindings.add((queue, exchange, routing_key))


@pytest.fixture()
def config() -> RabbitMQConfig:
    return RabbitMQConfig(
        url="amqp://guest:guest@localhost/",
        exchange="chronos.tasks",
        dead_letter_exchange="chronos.tasks.dlx",
        task_types=("send_email", "resize_image"),
    )


def test_declares_primary_and_dead_letter_exchange(config):
    channel = FakeChannel()
    declare_topology(channel, config)

    assert channel.exchanges["chronos.tasks"] == {"type": "topic", "durable": True}
    assert channel.exchanges["chronos.tasks.dlx"] == {"type": "topic", "durable": True}


def test_declares_durable_queue_per_task_type(config):
    channel = FakeChannel()
    declare_topology(channel, config)

    assert "chronos.tasks.send_email" in channel.queues
    assert "chronos.tasks.resize_image" in channel.queues
    assert channel.queues["chronos.tasks.send_email"]["durable"] is True


def test_primary_queue_has_dead_letter_arguments(config):
    channel = FakeChannel()
    declare_topology(channel, config)

    args = channel.queues["chronos.tasks.send_email"]["arguments"]
    assert args["x-dead-letter-exchange"] == "chronos.tasks.dlx"
    assert args["x-dead-letter-routing-key"] == "task.send_email"


def test_declares_dead_letter_queue_per_task_type(config):
    channel = FakeChannel()
    declare_topology(channel, config)

    assert "chronos.tasks.send_email.dlq" in channel.queues
    assert channel.queues["chronos.tasks.send_email.dlq"]["durable"] is True
    # the DLQ itself carries no further dead-lettering
    assert channel.queues["chronos.tasks.send_email.dlq"]["arguments"] == {}


def test_bindings_use_task_routing_key(config):
    channel = FakeChannel()
    declare_topology(channel, config)

    assert ("chronos.tasks.send_email", "chronos.tasks", "task.send_email") in channel.bindings
    assert (
        "chronos.tasks.send_email.dlq",
        "chronos.tasks.dlx",
        "task.send_email",
    ) in channel.bindings


def test_topology_declaration_is_idempotent(config):
    channel = FakeChannel()
    declare_topology(channel, config)
    first_exchanges = dict(channel.exchanges)
    first_queues = dict(channel.queues)
    first_bindings = set(channel.bindings)

    # Running it again (and again) must not raise, and must not produce
    # any different or duplicated topology.
    declare_topology(channel, config)
    declare_topology(channel, config)

    assert channel.exchanges == first_exchanges
    assert channel.queues == first_queues
    assert channel.bindings == first_bindings


def test_declare_task_type_is_idempotent_and_additive(config):
    channel = FakeChannel()
    declare_topology(channel, config)

    declare_task_type(channel, "generate_report", config)
    declare_task_type(channel, "generate_report", config)

    assert "chronos.tasks.generate_report" in channel.queues
    assert "chronos.tasks.generate_report.dlq" in channel.queues
    # existing task-type topology from declare_topology is untouched
    assert "chronos.tasks.send_email" in channel.queues


def test_naming_helpers_are_deterministic():
    assert routing_key_for_task_type("send_email") == "task.send_email"
    assert queue_name_for_task_type("send_email") == "chronos.tasks.send_email"
    assert (
        dead_letter_queue_name_for_task_type("send_email")
        == "chronos.tasks.send_email.dlq"
    )
    assert queue_name_for_task_type("send_email", exchange="other.x") == "other.x.send_email"
