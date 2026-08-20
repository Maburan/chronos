"""Fallback-poller configuration, sourced from environment variables --
same ``from_env()`` convention as ``mq.config.RabbitMQConfig``
/``RelayConfig``/``WorkerConfig``. Kept separate from ``mq/config.py``
because the fallback poller never touches RabbitMQ at all (see
``worker/fallback_poller.py``); ``mq/config.py`` stays scoped to
messaging concerns.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from mq.config import ConfigError


@dataclass(frozen=True)
class FallbackPollerConfig:
    """``worker_id`` must be a stable, externally supplied identifier for
    the same reason ``mq.config.WorkerConfig.worker_id`` is -- lease
    ownership fencing (``lease_owner``) is only meaningful with a
    consistent identity. Distinct from the RabbitMQ worker's own
    ``CHRONOS_WORKER_ID`` since the fallback poller is a separate process
    concern (handoff section 18: "Fallback polling handles: broker
    unavailable entirely").
    """

    worker_id: str
    lease_duration_seconds: float = 300.0
    poll_idle_sleep_seconds: float = 0.1
    # None -> worker.heartbeat.LeaseHeartbeat derives ~lease_duration/3 on
    # its own; only set explicitly to override that default.
    heartbeat_interval_seconds: Optional[float] = None

    @classmethod
    def from_env(cls) -> "FallbackPollerConfig":
        worker_id = os.environ.get("CHRONOS_FALLBACK_WORKER_ID")
        if not worker_id:
            raise ConfigError(
                "CHRONOS_FALLBACK_WORKER_ID is not set; the fallback poller "
                "must have a stable, externally configured worker_id, "
                "never a hard-coded or auto-generated one"
            )
        raw_heartbeat_interval = os.environ.get("CHRONOS_FALLBACK_HEARTBEAT_INTERVAL_SECONDS")
        return cls(
            worker_id=worker_id,
            lease_duration_seconds=float(
                os.environ.get("CHRONOS_FALLBACK_LEASE_SECONDS", "300.0")
            ),
            poll_idle_sleep_seconds=float(
                os.environ.get("CHRONOS_FALLBACK_POLL_IDLE_SLEEP_SECONDS", "0.1")
            ),
            heartbeat_interval_seconds=(
                float(raw_heartbeat_interval) if raw_heartbeat_interval else None
            ),
        )
