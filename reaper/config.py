"""Reaper configuration, sourced from environment variables -- same
``from_env()`` convention as ``mq.config.RelayConfig``. Kept in its
own module rather than ``mq/config.py`` since the reaper never touches
RabbitMQ at all -- it only reads/writes PostgreSQL (see
``reaper/reaper.py``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ReaperConfig:
    """Discovery/retry tuning. Defaults are conservative and
    deterministic -- see reaper/reaper.py for how they're used. No
    identity value is required here (unlike WorkerConfig/
    FallbackPollerConfig's worker_id) -- the reaper doesn't hold a lease
    of its own, it only reclaims expired ones (core.transitions
    .expire_lease takes no worker_id parameter)."""

    batch_size: int = 100
    poll_interval_seconds: float = 5.0
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "ReaperConfig":
        return cls(
            batch_size=int(os.environ.get("CHRONOS_REAPER_BATCH_SIZE", "100")),
            poll_interval_seconds=float(
                os.environ.get("CHRONOS_REAPER_POLL_INTERVAL_SECONDS", "5.0")
            ),
            initial_backoff_seconds=float(
                os.environ.get("CHRONOS_REAPER_INITIAL_BACKOFF_SECONDS", "1.0")
            ),
            max_backoff_seconds=float(
                os.environ.get("CHRONOS_REAPER_MAX_BACKOFF_SECONDS", "30.0")
            ),
        )
