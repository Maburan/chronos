"""Redis concurrency-limiter configuration, sourced from environment
variables -- same ``from_env()`` convention as every other
config class in this project (``mq.config.RabbitMQConfig``,
``worker.config.FallbackPollerConfig``, ``reaper.config.ReaperConfig``).

The limiter itself (``redis_limiter/limiter.py``) is entirely optional --
nothing in Chronos requires Redis to be configured or running. A
``TaskWorker`` constructed with no ``limiter`` behaves exactly as it did
before this phase.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping


class ConfigError(RuntimeError):
    """Raised when required Redis limiter configuration is missing."""


_LIMIT_OVERRIDE_PREFIX = "CHRONOS_REDIS_LIMITER_LIMIT_"


@dataclass(frozen=True)
class RedisLimiterConfig:
    """Concurrency-limiter tuning.

    ``default_limit`` applies to every task type with no explicit
    override. A per-task-type override is read from
    ``CHRONOS_REDIS_LIMITER_LIMIT_<TASK_TYPE>`` (task_type upper-cased --
    e.g. a task_type of ``send_email`` is overridden by
    ``CHRONOS_REDIS_LIMITER_LIMIT_SEND_EMAIL``). Lookup at call time
    upper-cases the task_type being checked, so the comparison is
    case-insensitive in both directions.

    ``slot_ttl_seconds`` is an ADMISSION-CONTROL APPROXIMATION, not a
    correctness mechanism -- see ``redis_limiter/limiter.py``'s module
    docstring for the full reasoning. It is NOT renewed by
    ``worker.heartbeat.LeaseHeartbeat`` (explicit scope decision for this
    phase); the default is sized comfortably above a typical task's
    expected execution time so a crashed worker's slot self-heals via the
    acquire script's own stale-member pruning, without needing active
    renewal. Tune it per deployment if task execution times differ
    significantly from the default assumption.

    ``connect_timeout_seconds``/``socket_timeout_seconds`` bound how long
    a single Redis operation can block before this module's fail-open
    handling kicks in -- without these, an unreachable-but-not-immediately-
    refused Redis (e.g. a silently dropped connection) could otherwise
    stall an acquire/release call for the OS's default TCP timeout
    (minutes), which would defeat the entire fail-open guarantee.
    """

    url: str
    default_limit: int = 10
    limit_overrides: Mapping[str, int] = field(default_factory=dict)
    slot_ttl_seconds: float = 600.0
    key_prefix: str = "chronos:concurrency"
    connect_timeout_seconds: float = 2.0
    socket_timeout_seconds: float = 2.0

    def limit_for(self, task_type: str) -> int:
        """The effective concurrency limit for ``task_type``: its
        override if one is configured, else ``default_limit``."""
        return self.limit_overrides.get(task_type.upper(), self.default_limit)

    @classmethod
    def from_env(cls) -> "RedisLimiterConfig":
        url = os.environ.get("CHRONOS_REDIS_URL")
        if not url:
            raise ConfigError(
                "CHRONOS_REDIS_URL is not set; the Redis concurrency "
                "limiter is optional -- only construct RedisLimiterConfig "
                "if you intend to use it"
            )

        overrides: dict[str, int] = {}
        for env_name, value in os.environ.items():
            if not env_name.startswith(_LIMIT_OVERRIDE_PREFIX):
                continue
            task_type = env_name[len(_LIMIT_OVERRIDE_PREFIX):]
            if not task_type:
                continue
            overrides[task_type.upper()] = int(value)

        return cls(
            url=url,
            default_limit=int(
                os.environ.get("CHRONOS_REDIS_LIMITER_DEFAULT_LIMIT", "10")
            ),
            limit_overrides=overrides,
            slot_ttl_seconds=float(
                os.environ.get("CHRONOS_REDIS_LIMITER_SLOT_TTL_SECONDS", "600.0")
            ),
            key_prefix=os.environ.get(
                "CHRONOS_REDIS_LIMITER_KEY_PREFIX", "chronos:concurrency"
            ),
            connect_timeout_seconds=float(
                os.environ.get("CHRONOS_REDIS_LIMITER_CONNECT_TIMEOUT_SECONDS", "2.0")
            ),
            socket_timeout_seconds=float(
                os.environ.get("CHRONOS_REDIS_LIMITER_SOCKET_TIMEOUT_SECONDS", "2.0")
            ),
        )
