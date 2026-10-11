"""Autoscaler process entrypoint.

    python -m autoscaler

Wires the pure ``Autoscaler`` control loop to its real dependencies: a
PostgreSQL pool (the authoritative backlog signal, via
``db.persistence.get_backlog_counts``) and a ``DockerComposeRuntime`` (the
replica-count actuator). Mirrors ``reaper/__main__.py``'s shape: construct
the existing pieces, register graceful SIGINT/SIGTERM shutdown, run the
loop forever.

Requires ``CHRONOS_DATABASE_URL`` (same variable every entrypoint reads).
To actually scale it also needs, mounted into this container, the Docker
socket (so the Compose CLI can talk to the host daemon) and the compose
file + project directory -- see docker-compose.yml's ``autoscaler``
service. Never touches RabbitMQ or Redis; never claims tasks or writes
task state.

Opt-in: does nothing unless ``CHRONOS_AUTOSCALER_ENABLED`` is truthy. When
disabled it logs once and idles (still shuts down cleanly on a signal), so
the service can sit in the compose file harmlessly until switched on.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

from autoscaler.autoscaler import Autoscaler
from autoscaler.config import AutoscalerConfig
from autoscaler.runtime import DockerComposeRuntime
from db import persistence
from db.connection import get_pool

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger(__name__)


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def _handle_signal(signum, _frame) -> None:
        logger.info("[autoscaler] received signal %s, shutting down gracefully", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)


def main() -> int:
    database_url = os.environ.get("CHRONOS_DATABASE_URL")
    if not database_url:
        print("CHRONOS_DATABASE_URL is not set", file=sys.stderr)
        return 1

    config = AutoscalerConfig.from_env()
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)

    if not config.enabled:
        logger.info(
            "[autoscaler] disabled (CHRONOS_AUTOSCALER_ENABLED is not set); "
            "idling. Set CHRONOS_AUTOSCALER_ENABLED=1 to activate."
        )
        stop_event.wait()
        logger.info("[autoscaler] stopped")
        return 0

    if not config.task_types:
        logger.warning(
            "[autoscaler] enabled but no task types configured "
            "(CHRONOS_AUTOSCALER_TASK_TYPES / CHRONOS_TASK_TYPES); nothing to do"
        )

    pool = get_pool(database_url)
    runtime = DockerComposeRuntime(
        project=config.compose_project,
        compose_file=config.compose_file,
        project_directory=config.project_directory,
        service_prefix=config.service_prefix,
    )

    def backlog_provider() -> dict[str, int]:
        counts = persistence.get_backlog_counts(pool)
        return {task_type: c["backlog"] for task_type, c in counts.items()}

    autoscaler = Autoscaler(
        backlog_provider=backlog_provider,
        runtime=runtime,
        policy=config.policy(),
        config=config,
    )

    try:
        autoscaler.run_forever(stop_event=stop_event)
    finally:
        logger.info("[autoscaler] stopped")
        pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
