"""Row <-> ``TaskInstance``/``TaskRecord`` conversion.

``core.transitions.TaskInstance`` is frozen and carries only the five
state-machine-relevant fields (``state``, ``attempt_count``, ``max_attempts``,
``lease_owner``, ``lease_expires_at``). It never gains a database identity
field -- ``TaskRecord`` below is where the DB-owned identity/metadata
columns (id, workflow_id, name, task_type, payload, timestamps) live
instead, wrapping a ``TaskInstance`` rather than extending it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

from core.transitions import TaskInstance, TaskState


@dataclass(frozen=True)
class TaskRecord:
    """DB-side view of a task row: identity/metadata plus the embedded
    ``TaskInstance`` that ``core/transitions.py`` actually operates on."""

    id: int
    workflow_id: int
    name: str
    task_type: str
    payload: Optional[dict]
    created_at: datetime
    updated_at: datetime
    instance: TaskInstance


def row_to_task_instance(row: Mapping[str, Any]) -> TaskInstance:
    """Build a ``TaskInstance`` from a ``tasks`` row (a dict-row result).

    This goes through ``TaskInstance.__post_init__`` on every read, which
    is a deliberate, free consistency check: if a row somehow violates the
    frozen module's own invariants (lease pairing, attempt bounds, ...),
    it fails loudly here rather than being silently trusted.
    """
    return TaskInstance(
        state=TaskState(row["state"]),
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
    )


def row_to_task_record(row: Mapping[str, Any]) -> TaskRecord:
    return TaskRecord(
        id=row["id"],
        workflow_id=row["workflow_id"],
        name=row["name"],
        task_type=row["task_type"],
        payload=row["payload"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        instance=row_to_task_instance(row),
    )


def task_instance_update_params(task_id: int, instance: TaskInstance) -> dict:
    """Parameters for ``UPDATE_TASK_STATE``.

    Writes only the mutable, transition-controlled fields (+ ``updated_at``,
    set server-side) -- never identity/metadata columns such as
    ``workflow_id``, ``name``, ``task_type``, or ``payload``, which a
    ``TaskInstance`` doesn't carry and a state transition never touches.
    """
    return {
        "task_id": task_id,
        "state": instance.state.value,
        "attempt_count": instance.attempt_count,
        "lease_owner": instance.lease_owner,
        "lease_expires_at": instance.lease_expires_at,
    }
