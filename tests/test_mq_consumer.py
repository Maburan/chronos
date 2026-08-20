"""Phase 4: RabbitMQConsumer tests, against a fake pika channel/connection
reproducing the real BlockingChannel.consume()/basic_qos()/basic_ack()/
basic_reject() contract (verified against the installed pika source) --
no real broker involved."""

from __future__ import annotations

from mq.connection import RabbitMQConsumer


class FakeMethod:
    def __init__(self, delivery_tag: int):
        self.delivery_tag = delivery_tag


class FakeConsumerChannel:
    """Mimics pika's BlockingChannel.consume(): a generator yielding
    (method, properties, body) tuples, (None, None, None) once queued
    messages are exhausted -- exactly matching the real
    inactivity_timeout-elapsed behavior."""

    def __init__(self, message_bodies: list[bytes]):
        self._message_bodies = list(message_bodies)
        self.qos_calls: list[int] = []
        self.consume_calls: list[dict] = []
        self.ack_calls: list[int] = []
        self.reject_calls: list[tuple[int, bool]] = []
        self.is_open = True
        self._next_tag = 1

    def basic_qos(self, *, prefetch_count: int) -> None:
        self.qos_calls.append(prefetch_count)

    def consume(self, queue, *, auto_ack=False, inactivity_timeout=None):
        self.consume_calls.append(
            {"queue": queue, "auto_ack": auto_ack, "inactivity_timeout": inactivity_timeout}
        )
        return self._generator()

    def _generator(self):
        for body in self._message_bodies:
            tag = self._next_tag
            self._next_tag += 1
            yield FakeMethod(tag), object(), body
        while True:
            yield None, None, None

    def basic_ack(self, delivery_tag: int) -> None:
        self.ack_calls.append(delivery_tag)

    def basic_reject(self, delivery_tag: int, *, requeue: bool) -> None:
        self.reject_calls.append((delivery_tag, requeue))

    def close(self) -> None:
        self.is_open = False


class FakeConsumerConnection:
    def __init__(self, channel: FakeConsumerChannel):
        self._channel = channel
        self.is_open = True

    def channel(self) -> FakeConsumerChannel:
        return self._channel

    def close(self) -> None:
        self.is_open = False


def test_consumer_sets_prefetch_and_manual_ack():
    channel = FakeConsumerChannel([])
    connection = FakeConsumerConnection(channel)

    RabbitMQConsumer(connection, queue="chronos.tasks.send_email", prefetch_count=7)

    assert channel.qos_calls == [7]
    assert channel.consume_calls == [
        {
            "queue": "chronos.tasks.send_email",
            "auto_ack": False,
            "inactivity_timeout": 1.0,
        }
    ]


def test_next_message_returns_body():
    channel = FakeConsumerChannel([b'{"task_id": 1}'])
    connection = FakeConsumerConnection(channel)
    consumer = RabbitMQConsumer(connection, queue="q", prefetch_count=1)

    message = consumer.next_message()

    assert message is not None
    assert message.body == b'{"task_id": 1}'


def test_next_message_returns_none_on_inactivity_timeout():
    channel = FakeConsumerChannel([])  # generator immediately yields (None, None, None)
    connection = FakeConsumerConnection(channel)
    consumer = RabbitMQConsumer(connection, queue="q", prefetch_count=1)

    assert consumer.next_message() is None


def test_ack_calls_basic_ack_with_correct_delivery_tag():
    channel = FakeConsumerChannel([b"body-1", b"body-2"])
    connection = FakeConsumerConnection(channel)
    consumer = RabbitMQConsumer(connection, queue="q", prefetch_count=1)

    first = consumer.next_message()
    second = consumer.next_message()
    second.ack()
    first.ack()

    # acked in the order called, with each message's own tag -- not
    # necessarily delivery order, proving tags are bound per-message.
    assert channel.ack_calls == [2, 1]


def test_reject_calls_basic_reject_with_requeue_false():
    channel = FakeConsumerChannel([b"malformed"])
    connection = FakeConsumerConnection(channel)
    consumer = RabbitMQConsumer(connection, queue="q", prefetch_count=1)

    message = consumer.next_message()
    message.reject(requeue=False)

    assert channel.reject_calls == [(1, False)]


def test_close_closes_channel_then_connection():
    channel = FakeConsumerChannel([])
    connection = FakeConsumerConnection(channel)
    consumer = RabbitMQConsumer(connection, queue="q", prefetch_count=1)

    consumer.close()

    assert channel.is_open is False
    assert connection.is_open is False
