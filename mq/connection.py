"""RabbitMQ connection handling: the real, pika-backed confirmed publisher
and the real, pika-backed manual-ack consumer.

This module is the only place that imports ``pika`` directly outside of
tests. Both ``relay/outbox_relay.py`` and ``worker/task_worker.py`` depend
only on the narrow protocols below (``ConfirmedPublisher`` /
``MessageSource``), never on pika itself, so their own tests can swap in a
fake with no real broker involved (see tests/test_outbox_relay.py and
tests/test_worker_task_worker.py).
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol

import pika
import pika.exceptions

from mq.config import RabbitMQConfig


class BrokerUnavailableError(Exception):
    """Raised when a publish attempt fails because the broker connection
    itself is unusable (refused, dropped mid-publish, channel closed by
    the broker, ...) -- distinct from an ordinary "not confirmed" outcome.
    The outbox relay treats this as a signal to back off before retrying,
    same as any other publish failure: the event is simply left
    unpublished either way (see relay/outbox_relay.py)."""


class ConfirmedPublisher(Protocol):
    """The one operation the outbox relay needs from a publisher: publish
    one message and report whether the broker confirmed it. Nothing about
    connections, channels, or reconnection leaks into this interface --
    that's exactly what lets tests use a trivial fake instead of a real
    broker."""

    def publish(self, *, routing_key: str, body: bytes) -> bool:
        """Publish ``body`` to the configured exchange with ``routing_key``
        and wait for a publisher confirm. Returns ``True`` if and only if
        the broker acked the publish; returns ``False`` if the broker
        explicitly rejected it (nack / unroutable). Raises
        ``BrokerUnavailableError`` if the connection/channel itself is
        unusable -- a genuinely different condition from an explicit
        per-message rejection."""
        ...


def connect(config: RabbitMQConfig) -> pika.BlockingConnection:
    """Open a new blocking connection to the broker described by
    ``config.url``. Raises ``pika.exceptions.AMQPError`` (or a subclass) on
    failure -- callers (the relay's retry loop) are expected to catch that
    and back off rather than this module hiding/retrying it itself; one
    place owns retry policy (see relay/outbox_relay.py's module docstring).
    """
    parameters = pika.URLParameters(config.url)
    return pika.BlockingConnection(parameters)


class RabbitMQPublisher:
    """A ``ConfirmedPublisher`` backed by a real pika channel with
    publisher confirms enabled (``confirm_delivery``). Persistent delivery
    mode is set on every publish, per the approved design (section 16 of
    the handoff): messages must survive a broker restart while queued.

    One instance owns one connection + one channel; it is not
    thread-safe (pika's ``BlockingConnection`` isn't either) and is meant
    to be used by a single relay loop.
    """

    def __init__(self, connection: pika.BlockingConnection, config: RabbitMQConfig) -> None:
        self._connection = connection
        self._config = config
        self._channel = connection.channel()
        self._channel.confirm_delivery()

    def publish(self, *, routing_key: str, body: bytes) -> bool:
        # IMPORTANT: pika's BlockingChannel.basic_publish, in
        # confirm_delivery mode, does NOT return a bool -- its return type
        # is None. On a broker Ack it simply returns (implicitly None);
        # on a broker Nack it raises NackError; on a mandatory-but-unroutable
        # publish it raises UnroutableError. The only correct way to read
        # "was this confirmed?" is therefore "did basic_publish raise?",
        # never its return value -- see mq/connection.py's own test
        # coverage in tests/test_outbox_relay.py /
        # tests/test_rabbitmq_topology.py's sibling file for a regression
        # test pinning this exact contract.
        try:
            self._channel.basic_publish(
                exchange=self._config.exchange,
                routing_key=routing_key,
                body=body,
                properties=pika.BasicProperties(
                    delivery_mode=pika.DeliveryMode.Persistent,
                    content_type="application/json",
                ),
                mandatory=True,
            )
        except (pika.exceptions.NackError, pika.exceptions.UnroutableError):
            # The broker explicitly rejected this message (Nack) or
            # couldn't route it (mandatory + Unroutable) -- not a
            # connection/channel failure, just "not confirmed". The relay
            # treats a False return exactly like a publish exception: the
            # outbox row is left unpublished and retried on a later pass.
            return False
        except pika.exceptions.AMQPError as exc:
            # Any other AMQP-level failure (connection dropped, channel
            # closed by the broker, etc.) is a genuine broker-unavailable
            # condition, distinct from an explicit per-message rejection.
            raise BrokerUnavailableError(str(exc)) from exc

        # basic_publish completed without raising: in confirm_delivery
        # mode that means the broker Ack'd the message. This method never
        # sets published_at itself -- that decision belongs entirely to
        # the relay, only after this returns True.
        return True

    def close(self) -> None:
        try:
            if self._channel.is_open:
                self._channel.close()
        finally:
            if self._connection.is_open:
                self._connection.close()

    def __enter__(self) -> "RabbitMQPublisher":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class ConsumedMessage:
    """One message pulled off a queue, not yet acknowledged.

    Wraps the delivery-tag bookkeeping a real pika channel needs for
    ``basic_ack``/``basic_reject`` behind two plain no-argument-ish
    methods, so worker code (``worker/task_worker.py``) never touches
    delivery tags, channels, or pika at all -- exactly the same shape of
    indirection ``ConfirmedPublisher`` gives the relay over ``pika``.
    """

    def __init__(
        self,
        body: bytes,
        *,
        ack_fn: Callable[[], None],
        reject_fn: Callable[[bool], None],
    ) -> None:
        self.body = body
        self._ack_fn = ack_fn
        self._reject_fn = reject_fn

    def ack(self) -> None:
        """Acknowledge this message: it will never be redelivered."""
        self._ack_fn()

    def reject(self, *, requeue: bool) -> None:
        """Reject this message. ``requeue=False`` is what routes a
        malformed message to its queue's dead-letter destination, per the
        ``x-dead-letter-exchange``/``x-dead-letter-routing-key`` arguments
        every primary queue was declared with (see mq/topology.py) --
        RabbitMQ does the DLX routing itself; nothing here re-publishes
        the message anywhere."""
        self._reject_fn(requeue)


class MessageSource(Protocol):
    """The one operation a worker needs from a consumer: the
    next available message, or ``None`` if none arrived within this
    source's own timeout. Nothing about connections, channels, consumer
    tags, or reconnection leaks into this interface -- exactly what lets
    tests use a trivial fake instead of a real broker (see
    tests/test_worker_task_worker.py)."""

    def next_message(self) -> Optional[ConsumedMessage]:
        """Return the next message, or ``None`` if none arrived before
        this source's configured inactivity timeout elapsed -- a normal,
        expected outcome (lets a polling loop periodically check a stop
        condition), never an error."""
        ...


class RabbitMQConsumer:
    """A ``MessageSource`` backed by a real pika channel: manual
    acknowledgement (``auto_ack=False``), a small prefetch count (so one
    slow task doesn't let the broker hand this worker an unbounded queue
    of other messages it can't work on yet), consuming exactly one named
    queue -- matching the one-worker-process-per-task-type-queue design
    (see ``mq.config.WorkerConfig``).

    Built on ``BlockingChannel.consume()``'s generator form rather than
    the callback-based ``basic_consume`` -- this lets ``next_message()``
    be a plain, synchronous, blocking-with-timeout call, which is what
    lets ``worker/task_worker.py``'s loop stay a simple
    fetch-one/handle-one shape (mirroring ``relay/outbox_relay.py``'s
    poll-one-batch/handle-it shape) instead of an event-loop/callback
    design.
    """

    def __init__(
        self,
        connection: pika.BlockingConnection,
        *,
        queue: str,
        prefetch_count: int,
        inactivity_timeout: float = 1.0,
    ) -> None:
        self._connection = connection
        self._channel = connection.channel()
        self._channel.basic_qos(prefetch_count=prefetch_count)
        self._queue = queue
        self._generator = self._channel.consume(
            queue, auto_ack=False, inactivity_timeout=inactivity_timeout
        )

    def next_message(self) -> Optional[ConsumedMessage]:
        method, _properties, body = next(self._generator)
        if method is None:
            # pika's documented inactivity_timeout behavior: no message
            # arrived within the timeout, not a failure.
            return None

        delivery_tag = method.delivery_tag

        def _ack() -> None:
            self._channel.basic_ack(delivery_tag)

        def _reject(requeue: bool) -> None:
            self._channel.basic_reject(delivery_tag, requeue=requeue)

        return ConsumedMessage(body, ack_fn=_ack, reject_fn=_reject)

    def close(self) -> None:
        try:
            if self._channel.is_open:
                self._channel.close()
        finally:
            if self._connection.is_open:
                self._connection.close()

    def __enter__(self) -> "RabbitMQConsumer":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
