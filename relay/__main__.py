"""Outbox relay process entrypoint.

    python -m relay

Wires the existing, unmodified ``relay.outbox_relay.OutboxRelay`` to a
real broker connection and a real database pool, declares the RabbitMQ
topology (idempotent -- safe regardless of whether the worker process has
already done this too), and runs it forever. No relay/outbox logic lives
here -- this module only constructs the existing pieces and calls
``run_forever()``.

Requires ``CHRONOS_DATABASE_URL`` (the same variable ``api/config.py``
uses for the API's own pool) in addition to the existing
``CHRONOS_RABBITMQ_URL``/``CHRONOS_RELAY_*`` variables
``RabbitMQConfig``/``RelayConfig`` already read.

Also provides graceful shutdown on SIGINT/SIGTERM using
``OutboxRelay.run_forever``'s existing ``stop_check`` parameter (no
change to the relay's own loop structure), and lightweight per-iteration
operational logging via its ``on_iteration`` callback.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

from db.connection import get_pool
from mq.config import RabbitMQConfig, RelayConfig
from mq.connection import RabbitMQPublisher, connect
from mq.topology import declare_topology
from relay.outbox_relay import OutboxRelay

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger(__name__)


def main() -> int:
    database_url = os.environ.get("CHRONOS_DATABASE_URL")
    if not database_url:
        print("CHRONOS_DATABASE_URL is not set", file=sys.stderr)
        return 1

    rabbitmq_config = RabbitMQConfig.from_env()
    relay_config = RelayConfig.from_env()

    connection = connect(rabbitmq_config)
    channel = connection.channel()
    declare_topology(channel, rabbitmq_config)

    publisher = RabbitMQPublisher(connection, rabbitmq_config)
    pool = get_pool(database_url)

    relay = OutboxRelay(pool, publisher, config=relay_config)

    # Graceful shutdown: the existing stop_check mechanism, nothing new
    # about the loop itself. Ctrl+C (SIGINT) is the reliable, portable way
    # to trigger this manually on Windows; SIGTERM is also registered for
    # real (non-Windows) deployments, though this process's own loop
    # structure is unaware of the distinction either way.
    stop_event = threading.Event()

    def _handle_signal(signum, _frame) -> None:
        logger.info("received signal %s, shutting down gracefully", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    def _log_iteration(result) -> None:
        logger.info(
            "outbox relay: iteration fetched=%d published=%d errors=%d",
            result.fetched,
            result.published,
            result.errors,
        )

    logger.info("outbox relay starting (exchange=%s)", rabbitmq_config.exchange)
    try:
        relay.run_forever(stop_check=stop_event.is_set, on_iteration=_log_iteration)
    finally:
        logger.info("outbox relay stopped")
        publisher.close()
        pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
