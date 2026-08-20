"""Worker process entrypoint.

    python -m worker

One worker process, pinned to exactly one task type
(``CHRONOS_WORKER_TASK_TYPE``, per the existing one-worker-per-task-type
design -- see ``mq.config.WorkerConfig``). Declares the RabbitMQ topology
(idempotent) plus this worker's own specific task-type queue, builds a
``TaskExecutorRegistry`` with the one demonstration task type registered
(``worker.demo_task.echo_task`` -- see that module's docstring; a real
deployment would register its own task types here instead), and runs the
existing, unmodified ``worker.task_worker.TaskWorker`` forever. No
claim/ack/execute/report logic lives here -- this module only constructs
the existing pieces.

Requires ``CHRONOS_DATABASE_URL`` in addition to the existing
``CHRONOS_RABBITMQ_URL``/``CHRONOS_WORKER_*`` variables
``RabbitMQConfig``/``WorkerConfig`` already read.

Also provides graceful shutdown on SIGINT/SIGTERM using
``TaskWorker.run_forever``'s existing ``stop_check`` parameter (no change
to the worker's own loop structure), and lightweight per-iteration
operational logging via its ``on_iteration`` callback.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from datetime import timedelta

from db.connection import get_pool
from mq.config import RabbitMQConfig, WorkerConfig
from mq.connection import RabbitMQConsumer, connect
from mq.topology import declare_task_type, declare_topology, queue_name_for_task_type
from worker.demo_task import ECHO_TASK_TYPE, echo_task
from worker.executor import TaskExecutorRegistry
from worker.task_worker import TaskWorker

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
    worker_config = WorkerConfig.from_env()

    connection = connect(rabbitmq_config)
    channel = connection.channel()
    declare_topology(channel, rabbitmq_config)
    declare_task_type(channel, worker_config.task_type, rabbitmq_config)

    queue = queue_name_for_task_type(worker_config.task_type, exchange=rabbitmq_config.exchange)
    consumer = RabbitMQConsumer(
        connection, queue=queue, prefetch_count=worker_config.prefetch_count
    )

    registry = TaskExecutorRegistry({ECHO_TASK_TYPE: echo_task})

    pool = get_pool(database_url)

    task_worker = TaskWorker(
        pool,
        consumer,
        registry,
        worker_id=worker_config.worker_id,
        lease_duration=timedelta(seconds=worker_config.lease_duration_seconds),
        heartbeat_interval_seconds=worker_config.heartbeat_interval_seconds,
    )

    # Graceful shutdown: the existing stop_check mechanism, nothing new
    # about the loop itself. Ctrl+C (SIGINT) is the reliable, portable way
    # to trigger this manually on Windows; SIGTERM is also registered for
    # real (non-Windows) deployments.
    stop_event = threading.Event()

    def _handle_signal(signum, _frame) -> None:
        logger.info("received signal %s, shutting down gracefully", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    def _log_iteration(got_message: bool) -> None:
        if got_message:
            logger.info("worker: iteration handled a message")

    logger.info(
        "worker starting (worker_id=%s task_type=%s queue=%s)",
        worker_config.worker_id,
        worker_config.task_type,
        queue,
    )
    try:
        task_worker.run_forever(
            stop_check=stop_event.is_set,
            idle_sleep_seconds=worker_config.poll_idle_sleep_seconds,
            on_iteration=_log_iteration,
        )
    finally:
        logger.info("worker stopped")
        consumer.close()
        pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
