"""Reaper process entrypoint.

    python -m reaper

Wires the existing, unmodified ``reaper.reaper.Reaper`` to a real database
pool and runs it forever. No lease-reclaim logic lives here -- this module
only constructs the existing pieces and calls ``run_forever()``, mirroring
``relay/__main__.py``'s shape exactly (this process is the one other
long-running loop in Chronos that previously had no standalone entrypoint
-- see HANDOVER.md).

Requires ``CHRONOS_DATABASE_URL`` (the same variable every other
entrypoint in this project reads) in addition to the existing
``CHRONOS_REAPER_*`` variables ``ReaperConfig`` already reads. Never
touches RabbitMQ or Redis -- the reaper only reads/writes PostgreSQL (see
``reaper/reaper.py``).

Also provides graceful shutdown on SIGINT/SIGTERM using
``Reaper.run_forever``'s existing ``stop_check`` parameter (no change to
the reaper's own loop structure). Unlike ``relay/__main__.py`` and
``worker/__main__.py``, ``Reaper.run_forever`` takes no ``on_iteration``
callback, so none is passed here -- per-pass results are already logged
from inside ``reaper/reaper.py`` itself.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

from db.connection import get_pool
from reaper.config import ReaperConfig
from reaper.reaper import Reaper

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger(__name__)


def main() -> int:
    database_url = os.environ.get("CHRONOS_DATABASE_URL")
    if not database_url:
        print("CHRONOS_DATABASE_URL is not set", file=sys.stderr)
        return 1

    reaper_config = ReaperConfig.from_env()
    pool = get_pool(database_url)
    reaper = Reaper(pool, config=reaper_config)

    # Graceful shutdown: the existing stop_check mechanism, nothing new
    # about the loop itself. Ctrl+C (SIGINT) is the reliable, portable way
    # to trigger this manually on Windows; SIGTERM is also registered for
    # real (non-Windows) deployments, including a container runtime's
    # `docker compose down`.
    stop_event = threading.Event()

    def _handle_signal(signum, _frame) -> None:
        logger.info("received signal %s, shutting down gracefully", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info(
        "reaper starting (poll_interval_seconds=%s batch_size=%s)",
        reaper_config.poll_interval_seconds,
        reaper_config.batch_size,
    )
    try:
        reaper.run_forever(stop_check=stop_event.is_set)
    finally:
        logger.info("reaper stopped")
        pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
