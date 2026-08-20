"""The Redis concurrency limiter: an optional, fail-open, per-task-type
admission gate.

    TaskWorker
        |
        | acquire(task_type, worker_id, task_id)   <-- BEFORE claim_task_by_id
        v
    Redis ZSET (this module)
        |
        | granted -> proceed to claim; denied -> ACK and skip, no claim attempt
        v
    claim_task_by_id() / execute_and_report()        <-- entirely unchanged
        |
        | release(task_type, worker_id, task_id)      <-- AFTER, always, in a finally
        v
    Redis ZSET

**Redis is never authoritative for task state.** This module never reads
or writes anything in PostgreSQL, never decides which task gets claimed
(only whether an attempt is made at all), and every operation fails open:
any Redis error, timeout, or unavailability is treated as "admission
granted" -- Chronos must keep making progress with no concurrency limit
enforced rather than ever block dispatch because Redis is down. See
``core/transitions.py``'s and ``db/persistence.py``'s docstrings for the
actual correctness authority (PostgreSQL's row locks and conditional
claims) -- nothing here changes or duplicates that.

The ZSET/Lua semaphore design follows the approved admission-control
design exactly:

  - one ZSET key per task_type
  - member: ``"<worker_id>:<task_id>"``
  - score: the slot's expiry timestamp (unix seconds)
  - acquire: an atomic Lua script that first prunes expired members
    (``ZREMRANGEBYSCORE ... -inf now``), then admits only if the
    (post-prune) cardinality is still under the configured limit
  - release: a plain ``ZREM``
  - crash recovery is TTL-based: a worker that dies while holding a slot
    never runs its ``release()`` call, but the slot's score naturally
    becomes prunable once its TTL elapses, reclaimed by the next
    ``acquire()`` call against that task_type -- no active renewal, no
    manual cleanup (see ``RedisLimiterConfig.slot_ttl_seconds`` for why
    this is a deliberate approximation, not a correctness guarantee).
"""

from __future__ import annotations

import logging
import time

import redis
from redis.exceptions import RedisError

from redis_limiter.config import RedisLimiterConfig

logger = logging.getLogger(__name__)

# KEYS[1] = the ZSET key for one task_type.
# ARGV[1] = current unix timestamp (the score cutoff for pruning expired
#           members -- anything with score <= this is stale).
# ARGV[2] = the configured limit for this task_type.
# ARGV[3] = this attempt's member ("worker_id:task_id").
# ARGV[4] = this slot's own expiry score (now + slot_ttl_seconds).
#
# Atomicity matters here specifically because the cardinality check and
# the add must happen as one indivisible step -- two workers concurrently
# both observing count < limit and both adding would exceed the limit.
# Redis executes Lua scripts single-threaded/atomically, which is what
# makes this safe without any additional locking.
ACQUIRE_SLOT_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
local count = redis.call('ZCARD', KEYS[1])
if count < tonumber(ARGV[2]) then
    redis.call('ZADD', KEYS[1], ARGV[4], ARGV[3])
    return 1
else
    return 0
end
"""


class TaskTypeLimiter:
    """Per-task-type admission control backed by one Redis ZSET per task
    type. One instance is safe to share across an entire worker process's
    lifetime (unlike ``worker.heartbeat.LeaseHeartbeat``, which is scoped
    to a single claimed task) -- it holds no per-task state of its own,
    only a Redis client.
    """

    def __init__(self, config: RedisLimiterConfig) -> None:
        self._config = config
        self._client = redis.Redis.from_url(
            config.url,
            decode_responses=True,
            socket_connect_timeout=config.connect_timeout_seconds,
            socket_timeout=config.socket_timeout_seconds,
            # RESP2, explicitly. redis-py defaults to negotiating RESP3 via
            # a HELLO handshake on connect, which only Redis 6.0+ supports.
            # Forcing protocol=2 skips that handshake entirely, so this
            # client works against any RESP2-speaking server (including
            # older/unofficial builds such as the Windows port this
            # project's local dev/test Redis instance runs) without
            # requiring a specific server version. This changes only how
            # the client talks to Redis -- it has no effect on the ZSET/
            # Lua admission logic, the fail-open behavior, or anything
            # else in this module.
            protocol=2,
        )
        self._acquire_script = self._client.register_script(ACQUIRE_SLOT_SCRIPT)

    def _key(self, task_type: str) -> str:
        return f"{self._config.key_prefix}:{task_type}"

    @staticmethod
    def _member(worker_id: str, task_id: int) -> str:
        return f"{worker_id}:{task_id}"

    def acquire(self, *, task_type: str, worker_id: str, task_id: int) -> bool:
        """Attempt to admit one more concurrent execution of ``task_type``.

        Returns ``True`` if admitted (a slot was granted) or if Redis is
        unavailable/erroring (fail-open -- never blocks dispatch just
        because Redis is unreachable). Returns ``False`` only when Redis
        is genuinely reachable and the configured limit for this
        task_type is currently at capacity. Never raises.
        """
        limit = self._config.limit_for(task_type)
        now = time.time()
        expiry_score = now + self._config.slot_ttl_seconds
        member = self._member(worker_id, task_id)
        key = self._key(task_type)

        try:
            granted = bool(self._acquire_script(keys=[key], args=[now, limit, member, expiry_score]))
        except RedisError:
            logger.warning(
                "redis_limiter: acquire failed for task_type=%s (Redis error); "
                "failing open (admission granted)",
                task_type,
                exc_info=True,
            )
            return True
        except Exception:  # noqa: BLE001 - any other connectivity issue must also fail open
            logger.warning(
                "redis_limiter: unexpected error acquiring slot for task_type=%s; "
                "failing open (admission granted)",
                task_type,
                exc_info=True,
            )
            return True

        if not granted:
            logger.info(
                "redis_limiter: admission denied for task_type=%s worker_id=%s "
                "task_id=%s (at limit=%d)",
                task_type,
                worker_id,
                task_id,
                limit,
            )
        return granted

    def release(self, *, task_type: str, worker_id: str, task_id: int) -> None:
        """Release a previously-acquired slot. Safe to call even if Redis
        is unreachable -- logs and returns rather than raising; a slot
        that can't be released this way still self-heals via TTL
        expiry."""
        key = self._key(task_type)
        member = self._member(worker_id, task_id)
        try:
            self._client.zrem(key, member)
        except RedisError:
            logger.warning(
                "redis_limiter: release failed for task_type=%s task_id=%s "
                "(Redis unavailable); slot will still expire via TTL",
                task_type,
                task_id,
                exc_info=True,
            )
        except Exception:  # noqa: BLE001 - never let a release failure propagate
            logger.warning(
                "redis_limiter: unexpected error releasing slot for task_type=%s "
                "task_id=%s; slot will still expire via TTL",
                task_type,
                task_id,
                exc_info=True,
            )
