"""RabbitMQ / relay configuration, sourced from environment variables.

No credentials or connection details are hard-coded anywhere in ``mq/`` or
``relay/`` -- everything callable from here reads its configuration
through :func:`RabbitMQConfig.from_env` / :func:`RelayConfig.from_env`,
mirroring how ``tests/conftest.py`` resolves ``CHRONOS_TEST_DATABASE_URL``
for Postgres rather than embedding a DSN in source.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


class ConfigError(RuntimeError):
    """Raised when required RabbitMQ/relay configuration is missing."""


@dataclass(frozen=True)
class RabbitMQConfig:
    """Everything the topology declaration and publisher need to talk to
    RabbitMQ. ``task_types`` is the set of task types the topology
    declares a durable queue for -- Chronos has no scheduler yet to
    discover task types dynamically, so this is the smallest clean way to
    make the queue set explicit and environment-configurable rather than
    guessed at or hard-coded.
    """

    url: str
    exchange: str = "chronos.tasks"
    dead_letter_exchange: str = "chronos.tasks.dlx"
    task_types: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls) -> "RabbitMQConfig":
        url = os.environ.get("CHRONOS_RABBITMQ_URL")
        if not url:
            raise ConfigError(
                "CHRONOS_RABBITMQ_URL is not set; RabbitMQ connection "
                "details must be provided via environment configuration, "
                "never hard-coded"
            )
        exchange = os.environ.get("CHRONOS_RABBITMQ_EXCHANGE", "chronos.tasks")
        dead_letter_exchange = os.environ.get(
            "CHRONOS_RABBITMQ_DLX", f"{exchange}.dlx"
        )
        raw_task_types = os.environ.get("CHRONOS_TASK_TYPES", "")
        task_types = tuple(
            t.strip() for t in raw_task_types.split(",") if t.strip()
        )
        return cls(
            url=url,
            exchange=exchange,
            dead_letter_exchange=dead_letter_exchange,
            task_types=task_types,
        )


@dataclass(frozen=True)
class RelayConfig:
    """Outbox relay polling/retry tuning. Defaults are conservative and
    deterministic -- see relay/outbox_relay.py for how they're used."""

    batch_size: int = 100
    poll_interval_seconds: float = 1.0
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "RelayConfig":
        return cls(
            batch_size=int(os.environ.get("CHRONOS_RELAY_BATCH_SIZE", "100")),
            poll_interval_seconds=float(
                os.environ.get("CHRONOS_RELAY_POLL_INTERVAL_SECONDS", "1.0")
            ),
            initial_backoff_seconds=float(
                os.environ.get("CHRONOS_RELAY_INITIAL_BACKOFF_SECONDS", "1.0")
            ),
            max_backoff_seconds=float(
                os.environ.get("CHRONOS_RELAY_MAX_BACKOFF_SECONDS", "30.0")
            ),
        )


@dataclass(frozen=True)
class WorkerConfig:
    """Worker tuning. ``worker_id`` must be a stable, externally
    supplied identifier -- never generated per-process here, since
    ``claim_task_by_id``'s lease-ownership fencing (``lease_owner``) is
    only meaningful if the same worker_id is used consistently across a
    process's lifetime (and, operationally, across restarts if you want
    logs/leases to correlate to "the same worker"). No distributed worker
    registry is introduced -- this is just a required, explicit,
    environment-configured value, exactly like ``CHRONOS_RABBITMQ_URL``.

    ``task_type`` fixes which single task-type queue this worker process
    consumes (one worker process per task-type queue, matching the
    topology's 1:1 per-type queue design -- see mq/topology.py).
    """

    worker_id: str
    task_type: str
    prefetch_count: int = 10
    lease_duration_seconds: float = 300.0
    poll_idle_sleep_seconds: float = 0.1
    # None -> worker.heartbeat.LeaseHeartbeat derives ~lease_duration/3 on
    # its own; only set explicitly to override that default.
    heartbeat_interval_seconds: Optional[float] = None

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        worker_id = os.environ.get("CHRONOS_WORKER_ID")
        if not worker_id:
            raise ConfigError(
                "CHRONOS_WORKER_ID is not set; every worker must have a "
                "stable, externally configured worker_id, never a "
                "hard-coded or auto-generated one"
            )
        task_type = os.environ.get("CHRONOS_WORKER_TASK_TYPE")
        if not task_type:
            raise ConfigError(
                "CHRONOS_WORKER_TASK_TYPE is not set; a worker "
                "process consumes exactly one task-type queue"
            )
        raw_heartbeat_interval = os.environ.get("CHRONOS_WORKER_HEARTBEAT_INTERVAL_SECONDS")
        return cls(
            worker_id=worker_id,
            task_type=task_type,
            prefetch_count=int(os.environ.get("CHRONOS_WORKER_PREFETCH", "10")),
            lease_duration_seconds=float(
                os.environ.get("CHRONOS_WORKER_LEASE_SECONDS", "300.0")
            ),
            poll_idle_sleep_seconds=float(
                os.environ.get("CHRONOS_WORKER_POLL_IDLE_SLEEP_SECONDS", "0.1")
            ),
            heartbeat_interval_seconds=(
                float(raw_heartbeat_interval) if raw_heartbeat_interval else None
            ),
        )
