"""Worker process entrypoint.

    python -m worker

One worker process, pinned to exactly one task type
(``CHRONOS_WORKER_TASK_TYPE``, per the existing one-worker-per-task-type
design -- see ``mq.config.WorkerConfig``). Declares the RabbitMQ topology
(idempotent) plus this worker's own specific task-type queue, builds a
``TaskExecutorRegistry`` with every registered task type
(``worker.tasks.TASK_TYPE_REGISTRY`` -- see that package's docstring for
what's registered and why), and runs the
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

Optionally wires in the Redis per-task-type admission limiter
(``redis_limiter.limiter.TaskTypeLimiter``), fully implemented and tested
since an earlier phase but never previously constructed by any real
entrypoint. Disabled by default -- set
``CHRONOS_REDIS_LIMITER_ENABLED=1`` to opt in (also requires
``CHRONOS_REDIS_URL``, read by ``RedisLimiterConfig.from_env()``). This
limits the maximum number of *concurrently executing* tasks of this
worker's task type across the whole fleet (default 10, per-type override
via ``CHRONOS_REDIS_LIMITER_LIMIT_<TASK_TYPE>``) -- it is optional,
fail-open admission control, never a correctness mechanism; see
``redis_limiter/limiter.py``'s module docstring. Omitting the env var
preserves the exact pre-existing behavior (no limiter passed to
``TaskWorker``, unlimited concurrency).
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
from worker.executor import TaskExecutorRegistry
from worker.task_worker import TaskWorker
from worker.tasks import TASK_TYPE_REGISTRY
from redis_limiter.config import RedisLimiterConfig
from redis_limiter.limiter import TaskTypeLimiter

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger(__name__)


def _build_limiter(worker_config: WorkerConfig) -> TaskTypeLimiter | None:
    """Opt-in only (see module docstring): returns ``None`` -- TaskWorker's
    existing default, unchanged behavior -- unless
    ``CHRONOS_REDIS_LIMITER_ENABLED=1`` is set. Split out from ``main()``
    so this one small piece of construction logic can be unit-tested
    without spinning up a real worker loop (see
    tests/test_worker_main_wiring.py)."""
    if os.environ.get("CHRONOS_REDIS_LIMITER_ENABLED") != "1":
        return None
    limiter_config = RedisLimiterConfig.from_env()
    logger.info(
        "worker: Redis admission limiter enabled (task_type=%s limit=%d)",
        worker_config.task_type,
        limiter_config.limit_for(worker_config.task_type),
    )
    return TaskTypeLimiter(limiter_config)


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

    # Every registered task type is handed to every worker process -- a
    # worker only ever actually receives dispatch hints for the one task
    # type its queue is bound to (worker_config.task_type, above), so
    # registering the full set here costs nothing and avoids each worker
    # needing its own bespoke registry-construction code.
    registry = TaskExecutorRegistry(TASK_TYPE_REGISTRY)

    pool = get_pool(database_url)

    limiter = _build_limiter(worker_config)

    task_worker = TaskWorker(
        pool,
        consumer,
        registry,
        worker_id=worker_config.worker_id,
        lease_duration=timedelta(seconds=worker_config.lease_duration_seconds),
        heartbeat_interval_seconds=worker_config.heartbeat_interval_seconds,
        limiter=limiter,
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
