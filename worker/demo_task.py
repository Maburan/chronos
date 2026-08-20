"""DEMONSTRATION TASK TYPE -- not a production workload.

``echo`` is the one task type Chronos ships and registers out of the box
(see ``worker/__main__.py``). It exists purely to prove the end-to-end
pipeline (HTTP API -> outbox -> RabbitMQ -> worker -> claim -> execute ->
report -> visible via GET) against a genuinely real, registered task type
instead of a test lambda -- and to drive the README's crash-recovery
demo. Deliberately trivial: no filesystem, network, Redis, or other
external side effect -- its only observable effect is a log line.

Its only behavior beyond that log line is two deterministic knobs used
to demonstrate specific recovery paths, both driven entirely by the
task's own ``payload`` (never a code change): ``fail_until_attempt``
(the reported-failure retry path, see tests/test_end_to_end.py) and
``sleep_seconds``/``sleep_until_attempt`` (the crash-recovery path, see
tests/test_crash_recovery.py and the README's "Crash Recovery Demo" --
it needs a task execution that blocks long enough for a real subprocess
to be killed mid-execution, and short enough not to slow the suite
down). A real deployment would register its own task types via
``worker.executor.TaskExecutorRegistry`` instead of relying on this one.
"""

from __future__ import annotations

import logging
import time

from db.models import TaskRecord

logger = logging.getLogger(__name__)

ECHO_TASK_TYPE = "echo"


def echo_task(record: TaskRecord) -> None:
    """Logs ``record.payload["message"]``.

    If ``record.payload["fail_until_attempt"]`` is set and the current
    attempt (``record.instance.attempt_count`` -- already incremented by
    the claim that led to this call) hasn't reached it yet, raises
    instead of succeeding, so the existing, unmodified
    ``report_task_failure``/retry machinery takes over. Once the
    threshold is reached, it succeeds normally.

    If ``record.payload["sleep_seconds"]`` is set, blocks for that long
    before doing anything else -- purely so a test can observe the task
    genuinely RUNNING and, e.g., kill the worker process mid-execution.
    Mirrors ``fail_until_attempt``'s shape: an optional
    ``sleep_until_attempt`` limits the sleep to attempts before that
    threshold (so a retried/redelivered attempt need not pay the same
    delay again). With no ``sleep_until_attempt``, the sleep applies to
    every attempt.
    """
    payload = record.payload or {}
    message = payload.get("message")

    sleep_seconds = payload.get("sleep_seconds")
    sleep_until_attempt = payload.get("sleep_until_attempt")
    if sleep_seconds is not None and (
        sleep_until_attempt is None or record.instance.attempt_count < sleep_until_attempt
    ):
        time.sleep(sleep_seconds)

    fail_until_attempt = payload.get("fail_until_attempt")
    if fail_until_attempt is not None and record.instance.attempt_count < fail_until_attempt:
        raise RuntimeError(
            f"echo_task: deliberate failure on attempt "
            f"{record.instance.attempt_count} (fail_until_attempt={fail_until_attempt})"
        )

    logger.info("echo_task: task_id=%s message=%r", record.id, message)
