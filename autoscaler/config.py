"""Autoscaler configuration, sourced from environment variables -- same
``from_env()`` convention as ReaperConfig/RelayConfig/WorkerConfig. Kept
in its own module (the autoscaler is its own process concern, like the
reaper), never a new/separate configuration system.

Every value has a documented, sensible default; a stock ``docker compose
up`` runs the autoscaler with these. Scale-UP is intentionally faster than
scale-DOWN: a small stabilization count lets a genuine spike add workers
quickly, while a longer cooldown keeps a briefly-empty queue from tearing
workers down (and then rebuilding them moments later) -- the classic
anti-thrash asymmetry.

    CHRONOS_AUTOSCALER_ENABLED                (default: false)
    CHRONOS_AUTOSCALER_INTERVAL               (default: 10.0  seconds)
    CHRONOS_AUTOSCALER_MIN_REPLICAS           (default: 1)
    CHRONOS_AUTOSCALER_MAX_REPLICAS           (default: 5)
    CHRONOS_AUTOSCALER_SCALE_UP_STABILIZATION (default: 2  observations)
    CHRONOS_AUTOSCALER_SCALE_DOWN_COOLDOWN    (default: 60.0 seconds)
    CHRONOS_AUTOSCALER_TASK_TYPES             (default: CHRONOS_TASK_TYPES)
    CHRONOS_AUTOSCALER_SERVICE_PREFIX         (default: "worker-")
    CHRONOS_AUTOSCALER_BACKLOG_TIERS          (default: "0:1,3:2,6:3,11:4,21:5")
    CHRONOS_AUTOSCALER_COMPOSE_PROJECT        (default: auto-detect)
    CHRONOS_AUTOSCALER_COMPOSE_FILE           (default: "docker-compose.yml")
    CHRONOS_AUTOSCALER_PROJECT_DIR            (default: "/app")
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from autoscaler.policy import DEFAULT_BACKLOG_TIERS, ScalingPolicy


class ConfigError(RuntimeError):
    """Raised when autoscaler configuration is present but malformed."""


def _parse_bool(raw: Optional[str]) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_tiers(raw: Optional[str]) -> tuple[tuple[int, int], ...]:
    """Parse ``"0:1,3:2,6:3"`` into sorted ``((0,1),(3,2),(6,3))``. An
    empty/unset value uses the documented default table."""
    if not raw or not raw.strip():
        return DEFAULT_BACKLOG_TIERS
    tiers: list[tuple[int, int]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            min_backlog_str, replicas_str = chunk.split(":")
            tiers.append((int(min_backlog_str), int(replicas_str)))
        except ValueError as exc:
            raise ConfigError(
                f"CHRONOS_AUTOSCALER_BACKLOG_TIERS chunk {chunk!r} is not "
                "'<min_backlog>:<replicas>'"
            ) from exc
    if not tiers:
        return DEFAULT_BACKLOG_TIERS
    tiers.sort(key=lambda t: t[0])
    return tuple(tiers)


@dataclass(frozen=True)
class AutoscalerConfig:
    enabled: bool = False
    interval_seconds: float = 10.0
    min_replicas: int = 1
    max_replicas: int = 5
    scale_up_stabilization: int = 2
    scale_down_cooldown_seconds: float = 60.0
    task_types: tuple[str, ...] = field(default_factory=tuple)
    service_prefix: str = "worker-"
    backlog_tiers: tuple[tuple[int, int], ...] = DEFAULT_BACKLOG_TIERS
    compose_project: Optional[str] = None
    compose_file: str = "docker-compose.yml"
    project_directory: str = "/app"

    def policy(self) -> ScalingPolicy:
        return ScalingPolicy(
            min_replicas=self.min_replicas,
            max_replicas=self.max_replicas,
            tiers=self.backlog_tiers,
        )

    @classmethod
    def from_env(cls) -> "AutoscalerConfig":
        # Task types default to the same list the relay declares topology
        # for (CHRONOS_TASK_TYPES) so the autoscaler manages exactly the
        # queues that exist, with no separate list to drift.
        raw_types = os.environ.get("CHRONOS_AUTOSCALER_TASK_TYPES") or os.environ.get(
            "CHRONOS_TASK_TYPES", ""
        )
        task_types = tuple(t.strip() for t in raw_types.split(",") if t.strip())

        min_replicas = int(os.environ.get("CHRONOS_AUTOSCALER_MIN_REPLICAS", "1"))
        max_replicas = int(os.environ.get("CHRONOS_AUTOSCALER_MAX_REPLICAS", "5"))
        if max_replicas < min_replicas:
            raise ConfigError(
                "CHRONOS_AUTOSCALER_MAX_REPLICAS must be >= "
                "CHRONOS_AUTOSCALER_MIN_REPLICAS"
            )

        compose_project = os.environ.get("CHRONOS_AUTOSCALER_COMPOSE_PROJECT") or None

        return cls(
            enabled=_parse_bool(os.environ.get("CHRONOS_AUTOSCALER_ENABLED")),
            interval_seconds=float(
                os.environ.get("CHRONOS_AUTOSCALER_INTERVAL", "10.0")
            ),
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            scale_up_stabilization=int(
                os.environ.get("CHRONOS_AUTOSCALER_SCALE_UP_STABILIZATION", "2")
            ),
            scale_down_cooldown_seconds=float(
                os.environ.get("CHRONOS_AUTOSCALER_SCALE_DOWN_COOLDOWN", "60.0")
            ),
            task_types=task_types,
            service_prefix=os.environ.get(
                "CHRONOS_AUTOSCALER_SERVICE_PREFIX", "worker-"
            ),
            backlog_tiers=_parse_tiers(
                os.environ.get("CHRONOS_AUTOSCALER_BACKLOG_TIERS")
            ),
            compose_project=compose_project,
            compose_file=os.environ.get(
                "CHRONOS_AUTOSCALER_COMPOSE_FILE", "docker-compose.yml"
            ),
            project_directory=os.environ.get("CHRONOS_AUTOSCALER_PROJECT_DIR", "/app"),
        )
