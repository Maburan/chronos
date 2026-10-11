"""Tests for worker/__main__.py::_build_limiter -- the opt-in Redis
admission-limiter wiring. Pure construction logic, no real worker loop
involved, and no real Redis connection is attempted (only
RedisLimiterConfig/TaskTypeLimiter construction is exercised -- Redis is
never contacted at construction time, only on first acquire/release, see
redis_limiter/limiter.py)."""

from __future__ import annotations

from mq.config import WorkerConfig
from redis_limiter.limiter import TaskTypeLimiter
from worker.__main__ import _build_limiter


def _worker_config(task_type: str = "echo") -> WorkerConfig:
    return WorkerConfig(worker_id="w1", task_type=task_type)


def test_build_limiter_returns_none_when_not_enabled(monkeypatch):
    monkeypatch.delenv("CHRONOS_REDIS_LIMITER_ENABLED", raising=False)
    assert _build_limiter(_worker_config()) is None


def test_build_limiter_returns_none_when_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("CHRONOS_REDIS_LIMITER_ENABLED", "0")
    assert _build_limiter(_worker_config()) is None


def test_build_limiter_constructs_a_real_limiter_when_enabled(monkeypatch):
    monkeypatch.setenv("CHRONOS_REDIS_LIMITER_ENABLED", "1")
    monkeypatch.setenv("CHRONOS_REDIS_URL", "redis://localhost:6379/0")

    limiter = _build_limiter(_worker_config())

    assert isinstance(limiter, TaskTypeLimiter)


def test_build_limiter_requires_redis_url_when_enabled(monkeypatch):
    monkeypatch.setenv("CHRONOS_REDIS_LIMITER_ENABLED", "1")
    monkeypatch.delenv("CHRONOS_REDIS_URL", raising=False)

    import pytest
    from redis_limiter.config import ConfigError

    with pytest.raises(ConfigError):
        _build_limiter(_worker_config())
