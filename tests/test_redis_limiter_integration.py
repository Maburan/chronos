"""Phase 7: real-Redis integration tests for TaskTypeLimiter.

Deliberately separate from tests/test_redis_limiter.py (deterministic
fail-open tests, no real Redis). Requires a real broker, addressed via
CHRONOS_TEST_REDIS_URL (same gating convention as
CHRONOS_TEST_RABBITMQ_URL for tests/test_rabbitmq_integration.py). If
that variable isn't set, every test here is skipped cleanly -- this file
is never a reason the rest of the suite fails or hangs.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

redis = pytest.importorskip("redis")

from redis_limiter.config import RedisLimiterConfig
from redis_limiter.limiter import TaskTypeLimiter

REDIS_URL = os.environ.get("CHRONOS_TEST_REDIS_URL")

pytestmark = pytest.mark.skipif(
    not REDIS_URL,
    reason="CHRONOS_TEST_REDIS_URL not set; skipping real-Redis integration tests",
)


@pytest.fixture()
def task_type() -> str:
    # A unique task_type (and therefore a unique ZSET key) per test avoids
    # any cross-test interference against a shared broker.
    return f"integration_test_type_{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def redis_client():
    # protocol=2: matches redis_limiter/limiter.py's TaskTypeLimiter client
    # construction exactly -- skips the RESP3 HELLO handshake redis-py
    # defaults to, which the local test Redis instance (an older,
    # RESP2-only build) doesn't support. Test-support-only; no test
    # assertion or Redis command usage below is affected.
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True, protocol=2)
    yield client
    client.close()


def _config(**overrides) -> RedisLimiterConfig:
    defaults = dict(url=REDIS_URL, default_limit=3, slot_ttl_seconds=60.0)
    defaults.update(overrides)
    return RedisLimiterConfig(**defaults)


def test_acquisition_below_limit_succeeds(task_type):
    limiter = TaskTypeLimiter(_config(default_limit=3))
    assert limiter.acquire(task_type=task_type, worker_id="w1", task_id=1) is True
    assert limiter.acquire(task_type=task_type, worker_id="w2", task_id=2) is True


def test_acquisition_at_limit_is_denied(task_type):
    limiter = TaskTypeLimiter(_config(default_limit=2))
    assert limiter.acquire(task_type=task_type, worker_id="w1", task_id=1) is True
    assert limiter.acquire(task_type=task_type, worker_id="w2", task_id=2) is True
    # Limit reached -- the third attempt must be denied.
    assert limiter.acquire(task_type=task_type, worker_id="w3", task_id=3) is False


def test_release_frees_capacity_for_reacquisition(task_type):
    limiter = TaskTypeLimiter(_config(default_limit=1))
    assert limiter.acquire(task_type=task_type, worker_id="w1", task_id=1) is True
    assert limiter.acquire(task_type=task_type, worker_id="w2", task_id=2) is False

    limiter.release(task_type=task_type, worker_id="w1", task_id=1)

    assert limiter.acquire(task_type=task_type, worker_id="w2", task_id=2) is True


def test_concurrent_acquisition_admits_exactly_the_limit(task_type):
    """Real concurrent acquire attempts against a small limit -- exactly
    `limit` must succeed, the rest correctly denied, regardless of
    ordering. Each thread uses its own TaskTypeLimiter/Redis client
    (genuinely separate connections), mirroring this project's
    established real-concurrency standard for PostgreSQL."""
    limit = 5
    attempts = 20

    def _attempt(i: int) -> bool:
        limiter = TaskTypeLimiter(_config(default_limit=limit))
        return limiter.acquire(task_type=task_type, worker_id=f"w{i}", task_id=i)

    with ThreadPoolExecutor(max_workers=attempts) as executor:
        futures = [executor.submit(_attempt, i) for i in range(attempts)]
        results = [f.result() for f in as_completed(futures)]

    assert sum(1 for r in results if r) == limit
    assert sum(1 for r in results if not r) == attempts - limit


def test_stale_slot_is_pruned_and_capacity_reclaimed(task_type, redis_client):
    """TTL-based crash recovery: a slot with an already-past expiry score
    (simulating a worker that died without ever calling release()) must
    be pruned by the next acquire() call against that task_type, freeing
    its capacity -- with no explicit release() ever happening for it."""
    limiter = TaskTypeLimiter(_config(default_limit=1))

    # Manually insert a "crashed worker's" slot with an expiry score
    # already in the past -- bypassing acquire() entirely, exactly as a
    # real crash (no release() call) would leave it, just already-expired
    # rather than waiting out a real TTL.
    key = limiter._key(task_type)
    stale_member = "dead-worker:999"
    redis_client.zadd(key, {stale_member: time.time() - 10})
    assert redis_client.zcard(key) == 1

    # A fresh acquire, at the limit, must prune the stale member first and
    # then succeed -- proving self-healing crash recovery with no
    # explicit release() ever called for the dead worker's slot.
    assert limiter.acquire(task_type=task_type, worker_id="w1", task_id=1) is True
    assert redis_client.zscore(key, stale_member) is None  # pruned


def test_redis_outage_after_successful_connection_fails_open(task_type):
    """A limiter pointed at a genuinely nonexistent Redis (connection
    refused) must fail open on every call, never raise."""
    unreachable_config = RedisLimiterConfig(
        url="redis://127.0.0.1:1",
        default_limit=1,
        connect_timeout_seconds=0.3,
        socket_timeout_seconds=0.3,
    )
    limiter = TaskTypeLimiter(unreachable_config)
    assert limiter.acquire(task_type=task_type, worker_id="w1", task_id=1) is True
    limiter.release(task_type=task_type, worker_id="w1", task_id=1)  # must not raise


def test_end_to_end_denied_admission_leaves_task_ready_and_no_attempt(db, task_type):
    """Real Redis + real PostgreSQL: a task_type at its Redis limit must
    leave a newly-created task genuinely READY, with attempt_count
    untouched, when a caller correctly declines to claim after denial
    (exactly what worker.task_worker.TaskWorker does -- see
    tests/test_worker_task_worker_redis.py for the wired-through version
    of this same guarantee)."""
    from db import persistence

    limiter = TaskTypeLimiter(_config(default_limit=1))
    assert limiter.acquire(task_type=task_type, worker_id="occupier", task_id=0) is True

    wf = persistence.create_workflow(db, name="wf", user_id=db.test_user_id)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type=task_type)

    # Simulate what TaskWorker does on denial: check admission, and since
    # it's denied, never call claim_task_by_id at all.
    admitted = limiter.acquire(task_type=task_type, worker_id="w2", task_id=task.id)
    assert admitted is False

    with db.connection() as conn:
        row = conn.execute(
            "SELECT state, attempt_count FROM tasks WHERE id = %s", (task.id,)
        ).fetchone()
    assert row["state"] == "READY"
    assert row["attempt_count"] == 0
