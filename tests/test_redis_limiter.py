"""Phase 7: TaskTypeLimiter unit/fail-open tests. Deterministic --
these specifically test behavior when Redis is unreachable, so no real
Redis is used here at all (see tests/test_redis_limiter_integration.py
for real-Redis behavior)."""

from __future__ import annotations

from redis_limiter.config import RedisLimiterConfig
from redis_limiter.limiter import TaskTypeLimiter


def _config(**overrides) -> RedisLimiterConfig:
    defaults = dict(
        url="redis://127.0.0.1:1",  # deliberately unreachable: nothing listens here
        default_limit=5,
        slot_ttl_seconds=60.0,
        connect_timeout_seconds=0.2,
        socket_timeout_seconds=0.2,
    )
    defaults.update(overrides)
    return RedisLimiterConfig(**defaults)


def test_acquire_fails_open_when_redis_unreachable():
    limiter = TaskTypeLimiter(_config())
    result = limiter.acquire(task_type="send_email", worker_id="w1", task_id=1)
    assert result is True  # admission granted despite Redis being unreachable


def test_release_does_not_raise_when_redis_unreachable():
    limiter = TaskTypeLimiter(_config())
    # Must not raise -- a release failure is logged and otherwise ignored;
    # the slot (if it ever existed) still self-heals via TTL.
    limiter.release(task_type="send_email", worker_id="w1", task_id=1)


def test_multiple_acquires_all_fail_open_when_unreachable():
    limiter = TaskTypeLimiter(_config(default_limit=1))
    # Even though the configured limit is 1, an unreachable Redis must
    # never enforce it -- every attempt is granted.
    assert limiter.acquire(task_type="t", worker_id="w1", task_id=1) is True
    assert limiter.acquire(task_type="t", worker_id="w2", task_id=2) is True
    assert limiter.acquire(task_type="t", worker_id="w3", task_id=3) is True


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_limit_for_uses_default_when_no_override():
    config = RedisLimiterConfig(url="redis://localhost:6379", default_limit=7)
    assert config.limit_for("anything") == 7


def test_limit_for_uses_override_case_insensitively():
    config = RedisLimiterConfig(
        url="redis://localhost:6379",
        default_limit=7,
        limit_overrides={"SEND_EMAIL": 2},
    )
    assert config.limit_for("send_email") == 2
    assert config.limit_for("SEND_EMAIL") == 2
    assert config.limit_for("SeNd_EmAiL") == 2
    assert config.limit_for("other_type") == 7


def test_from_env_requires_url(monkeypatch):
    monkeypatch.delenv("CHRONOS_REDIS_URL", raising=False)
    import pytest

    from redis_limiter.config import ConfigError

    with pytest.raises(ConfigError):
        RedisLimiterConfig.from_env()


def test_from_env_reads_default_limit_and_overrides(monkeypatch):
    monkeypatch.setenv("CHRONOS_REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("CHRONOS_REDIS_LIMITER_DEFAULT_LIMIT", "3")
    monkeypatch.setenv("CHRONOS_REDIS_LIMITER_LIMIT_SEND_EMAIL", "9")
    monkeypatch.setenv("CHRONOS_REDIS_LIMITER_SLOT_TTL_SECONDS", "45")

    config = RedisLimiterConfig.from_env()

    assert config.url == "redis://localhost:6379"
    assert config.default_limit == 3
    assert config.limit_for("send_email") == 9
    assert config.limit_for("other") == 3
    assert config.slot_ttl_seconds == 45.0
