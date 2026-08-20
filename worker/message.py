"""Parsing/validation for the TASK_READY dispatch-hint message body --
the exact shape ``relay/outbox_relay.py`` publishes:

    {"event_id": ..., "task_id": ..., "workflow_id": ..., "task_type": ...}

Nothing here decides task state. ``task_id`` is the only field ever used
downstream, and only as the target of ``persistence.claim_task_by_id``
(see worker/task_worker.py). ``workflow_id`` and ``task_type`` from the
message are carried through for logging only -- once a claim succeeds,
the returned ``TaskRecord`` is the sole authoritative source for those
same facts (handoff section 9's dispatch-hint invariant, applied all the
way to the worker).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class InvalidDispatchHintError(Exception):
    """Raised when a RabbitMQ message body doesn't parse as a valid
    TASK_READY dispatch hint -- non-JSON, non-object, missing fields, or
    wrong field types. The worker's only response to this is to reject the
    message (routing it to its task type's dead-letter queue via the
    existing RabbitMQ topology, see mq/topology.py) -- never to crash the
    consume loop and never to attempt any PostgreSQL claim."""


@dataclass(frozen=True)
class DispatchHint:
    """The validated fields of a TASK_READY message."""

    event_id: int
    task_id: int
    workflow_id: int
    task_type: str


_REQUIRED_INT_FIELDS = ("event_id", "task_id", "workflow_id")


def _require_int_field(data: dict, field: str) -> int:
    if field not in data:
        raise InvalidDispatchHintError(f"missing required field: {field}")
    value = data[field]
    # bool is an int subclass in Python -- explicitly excluded so a stray
    # `true`/`false` in the JSON isn't silently accepted as 0/1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidDispatchHintError(
            f"field {field!r} must be an integer, got {type(value).__name__}"
        )
    return value


def parse_dispatch_hint(body: bytes) -> DispatchHint:
    """Parse and validate a raw message body against the exact shape
    ``relay/outbox_relay.py`` publishes. Raises ``InvalidDispatchHintError``
    for anything that doesn't match. Never raises anything else -- any
    other exception here would be a bug in this function, not a signal
    about the message."""
    try:
        data: Any = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidDispatchHintError(f"message body is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise InvalidDispatchHintError(
            f"message body must be a JSON object, got {type(data).__name__}"
        )

    event_id = _require_int_field(data, "event_id")
    task_id = _require_int_field(data, "task_id")
    workflow_id = _require_int_field(data, "workflow_id")

    if "task_type" not in data:
        raise InvalidDispatchHintError("missing required field: task_type")
    task_type = data["task_type"]
    if not isinstance(task_type, str) or not task_type:
        raise InvalidDispatchHintError("field 'task_type' must be a non-empty string")

    return DispatchHint(
        event_id=event_id, task_id=task_id, workflow_id=workflow_id, task_type=task_type
    )
