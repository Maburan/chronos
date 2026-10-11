"""``sleep`` task type: blocks for a configured duration, then succeeds.
Useful for demonstrating long-running tasks, crash-recovery timing, and
workflow parallelism without any external dependency -- a general-purpose
complement to ``echo``'s ``sleep_seconds`` knob (see
``worker/demo_task.py``), registered as its own task type rather than an
echo-only parameter.
"""

from __future__ import annotations

import time

from db.models import TaskRecord
from worker.tasks.errors import InvalidTaskPayloadError

SLEEP_TASK_TYPE = "sleep"

# A task type must never be able to starve a worker's execution slot
# indefinitely just because a payload asked for an absurd duration --
# this is an admission-time cap, not a claim about how long any lease
# happens to be configured for.
_MAX_SLEEP_SECONDS = 300


def sleep_task(record: TaskRecord) -> None:
    payload = record.payload or {}
    seconds = payload.get("seconds")
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds < 0:
        raise InvalidTaskPayloadError("sleep task requires a non-negative numeric 'seconds'")
    if seconds > _MAX_SLEEP_SECONDS:
        raise InvalidTaskPayloadError(
            f"'seconds' ({seconds}) exceeds the maximum allowed ({_MAX_SLEEP_SECONDS})"
        )
    time.sleep(seconds)
