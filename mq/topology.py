"""The approved RabbitMQ topology: one durable topic exchange, one durable
per-task-type queue, and a dead-letter exchange/queue pair for poison
messages.

Nothing here is authoritative. A message on any of these queues means
"task X may be available; go attempt a PostgreSQL claim" -- never
"task X is in this state." See ``CHRONOS_IMPLEMENTATION_HANDOFF.md``
sections 1, 14, 16.

Naming convention (deterministic, documented here as the single source of
truth for it):

  exchange                 chronos.tasks                    (topic, durable)
  dead-letter exchange     chronos.tasks.dlx                (topic, durable)
  routing key              task.<task_type>
  queue                    chronos.tasks.<task_type>         (durable)
  dead-letter queue        chronos.tasks.<task_type>.dlq      (durable)

Each primary queue is declared with ``x-dead-letter-exchange`` pointing at
the dead-letter exchange and ``x-dead-letter-routing-key`` set to that same
``task.<task_type>`` key, so a poison message (rejected/nacked/expired) is
routed to that task type's own DLQ rather than a shared catch-all -- the
same per-type separation the primary topology uses. Worker-side DLQ
handling (consuming from, inspecting, or replaying the DLQ) is not built
yet -- this module only declares the topology.

Declaration is idempotent: every ``exchange_declare``/``queue_declare``/
``queue_bind`` call always passes the same arguments for a given name, and
RabbitMQ's own AMQP semantics make redeclaring an existing
exchange/queue/binding with identical arguments a no-op. Running
``declare_topology`` twice (or a hundred times) against the same broker
produces exactly one exchange, one DLX, and one queue pair per task type --
never a duplicate, never an error.
"""

from __future__ import annotations

from typing import Protocol

from mq.config import RabbitMQConfig


class DeclaresTopology(Protocol):
    """The subset of ``pika.adapters.blocking_connection.BlockingChannel``
    this module needs. Kept narrow and explicit so topology declaration can
    be exercised against a lightweight fake in tests without a real broker
    (see tests/test_rabbitmq_topology.py)."""

    def exchange_declare(
        self, exchange: str, exchange_type: str, durable: bool
    ) -> None: ...

    def queue_declare(
        self, queue: str, durable: bool, arguments: dict
    ) -> None: ...

    def queue_bind(
        self, queue: str, exchange: str, routing_key: str
    ) -> None: ...


def routing_key_for_task_type(task_type: str) -> str:
    """``task.<task_type>`` -- the one routing-key shape used everywhere:
    outbox relay publishes, primary-queue bindings, and dead-letter
    re-routing all derive it from this single function."""
    return f"task.{task_type}"


def queue_name_for_task_type(task_type: str, *, exchange: str = "chronos.tasks") -> str:
    """``<exchange>.<task_type>`` -- e.g. ``chronos.tasks.send_email``."""
    return f"{exchange}.{task_type}"


def dead_letter_queue_name_for_task_type(
    task_type: str, *, exchange: str = "chronos.tasks"
) -> str:
    """``<exchange>.<task_type>.dlq`` -- e.g. ``chronos.tasks.send_email.dlq``."""
    return f"{queue_name_for_task_type(task_type, exchange=exchange)}.dlq"


def declare_topology(channel: DeclaresTopology, config: RabbitMQConfig) -> None:
    """Declare the full topology on an already-open channel.

    Order matters only in that exchanges must exist before anything binds
    to them: primary exchange, then DLX, then per-task-type queue + DLQ +
    bindings. Every call is a plain declare/bind with fixed arguments, so
    repeating this whole function against the same broker is always safe
    (see module docstring).
    """
    channel.exchange_declare(
        exchange=config.exchange, exchange_type="topic", durable=True
    )
    channel.exchange_declare(
        exchange=config.dead_letter_exchange, exchange_type="topic", durable=True
    )

    for task_type in config.task_types:
        _declare_task_type_topology(channel, task_type, config)


def _declare_task_type_topology(
    channel: DeclaresTopology, task_type: str, config: RabbitMQConfig
) -> None:
    routing_key = routing_key_for_task_type(task_type)
    queue = queue_name_for_task_type(task_type, exchange=config.exchange)
    dlq = dead_letter_queue_name_for_task_type(task_type, exchange=config.exchange)

    # Dead-letter queue first: the primary queue's arguments reference the
    # DLX/routing key, but nothing requires the DLQ to already be bound for
    # the primary declare to succeed -- declared first anyway, for
    # readability (poison-message destination exists before the queue that
    # can route messages to it).
    channel.queue_declare(queue=dlq, durable=True, arguments={})
    channel.queue_bind(
        queue=dlq, exchange=config.dead_letter_exchange, routing_key=routing_key
    )

    channel.queue_declare(
        queue=queue,
        durable=True,
        arguments={
            "x-dead-letter-exchange": config.dead_letter_exchange,
            "x-dead-letter-routing-key": routing_key,
        },
    )
    channel.queue_bind(queue=queue, exchange=config.exchange, routing_key=routing_key)


def declare_task_type(channel: DeclaresTopology, task_type: str, config: RabbitMQConfig) -> None:
    """Declare (idempotently) the queue/DLQ/bindings for a single task type
    that wasn't in ``config.task_types`` at startup. The primary and DLX
    exchanges must already exist (call ``declare_topology`` first)."""
    _declare_task_type_topology(channel, task_type, config)
