"""Regression tests pinning ``mq.connection.RabbitMQPublisher``'s exact
reading of pika's ``BlockingChannel.basic_publish`` contract in
confirm-delivery mode.

This is the contract the Phase 3 audit found broken: ``basic_publish``
returns ``None`` on a successful, broker-confirmed publish and raises
(``NackError`` / ``UnroutableError``) on an unsuccessful one -- it never
returns a bool. These tests use a fake channel/connection (no real broker)
that reproduces exactly that behavior, so a future regression back to
"trust the return value" is caught here rather than only against a real
broker.
"""

from __future__ import annotations

import pika
import pika.exceptions
import pytest

from mq.config import RabbitMQConfig
from mq.connection import BrokerUnavailableError, RabbitMQPublisher


class FakeChannel:
    """Mimics just the pieces of ``pika.BlockingChannel`` this module
    touches. ``basic_publish`` is scripted per test: it either returns
    ``None`` (the real success case) or raises whatever the test wants,
    matching pika's actual confirm-mode contract rather than a bool."""

    def __init__(self, *, raise_exc: Exception | None = None):
        self.confirm_delivery_called = False
        self.publish_calls: list[dict] = []
        self._raise_exc = raise_exc
        self.is_open = True
        self.closed = False

    def confirm_delivery(self) -> None:
        self.confirm_delivery_called = True

    def basic_publish(self, *, exchange, routing_key, body, properties, mandatory):
        self.publish_calls.append(
            {
                "exchange": exchange,
                "routing_key": routing_key,
                "body": body,
                "properties": properties,
                "mandatory": mandatory,
            }
        )
        if self._raise_exc is not None:
            raise self._raise_exc
        return None  # pika's real, documented return value on success

    def close(self) -> None:
        self.closed = True
        self.is_open = False


class FakeConnection:
    def __init__(self, channel: FakeChannel):
        self._channel = channel
        self.is_open = True
        self.closed = False

    def channel(self) -> FakeChannel:
        return self._channel

    def close(self) -> None:
        self.closed = True
        self.is_open = False


def _config() -> RabbitMQConfig:
    return RabbitMQConfig(url="amqp://guest:guest@localhost/", exchange="chronos.tasks")


# ---------------------------------------------------------------------------
# the core regression: basic_publish returning None on success
# ---------------------------------------------------------------------------


def test_publish_returns_true_when_basic_publish_completes_without_raising():
    """Pins the exact bug found in audit: basic_publish's successful return
    value is None, not True -- publish() must not rely on that return value
    and must still report True."""
    channel = FakeChannel(raise_exc=None)
    connection = FakeConnection(channel)
    publisher = RabbitMQPublisher(connection, _config())

    result = publisher.publish(routing_key="task.send_email", body=b"{}")

    assert result is True
    # sanity: basic_publish really did return None, and that's fine
    assert channel.publish_calls[0]["routing_key"] == "task.send_email"


def test_confirm_delivery_is_enabled_on_construction():
    channel = FakeChannel()
    connection = FakeConnection(channel)
    RabbitMQPublisher(connection, _config())

    assert channel.confirm_delivery_called is True


def test_publish_sends_persistent_delivery_mode():
    channel = FakeChannel()
    connection = FakeConnection(channel)
    publisher = RabbitMQPublisher(connection, _config())

    publisher.publish(routing_key="task.send_email", body=b"{}")

    props = channel.publish_calls[0]["properties"]
    assert props.delivery_mode == pika.DeliveryMode.Persistent.value


# ---------------------------------------------------------------------------
# explicit broker rejection: NackError / UnroutableError -> False, not raised
# ---------------------------------------------------------------------------


def test_nack_error_returns_false_not_broker_unavailable():
    channel = FakeChannel(raise_exc=pika.exceptions.NackError([]))
    connection = FakeConnection(channel)
    publisher = RabbitMQPublisher(connection, _config())

    result = publisher.publish(routing_key="task.send_email", body=b"{}")

    assert result is False


def test_unroutable_error_returns_false_not_broker_unavailable():
    channel = FakeChannel(raise_exc=pika.exceptions.UnroutableError([]))
    connection = FakeConnection(channel)
    publisher = RabbitMQPublisher(connection, _config())

    result = publisher.publish(routing_key="task.send_email", body=b"{}")

    assert result is False


# ---------------------------------------------------------------------------
# genuine connection/channel failure -> BrokerUnavailableError
# ---------------------------------------------------------------------------


def test_connection_level_amqp_error_raises_broker_unavailable():
    channel = FakeChannel(raise_exc=pika.exceptions.AMQPConnectionError("connection reset"))
    connection = FakeConnection(channel)
    publisher = RabbitMQPublisher(connection, _config())

    with pytest.raises(BrokerUnavailableError):
        publisher.publish(routing_key="task.send_email", body=b"{}")


def test_channel_closed_by_broker_raises_broker_unavailable():
    channel = FakeChannel(raise_exc=pika.exceptions.ChannelClosedByBroker(320, "CONNECTION_FORCED"))
    connection = FakeConnection(channel)
    publisher = RabbitMQPublisher(connection, _config())

    with pytest.raises(BrokerUnavailableError):
        publisher.publish(routing_key="task.send_email", body=b"{}")


# ---------------------------------------------------------------------------
# resource handling
# ---------------------------------------------------------------------------


def test_close_closes_channel_then_connection():
    channel = FakeChannel()
    connection = FakeConnection(channel)
    publisher = RabbitMQPublisher(connection, _config())

    publisher.close()

    assert channel.closed is True
    assert connection.closed is True
