"""Request/response shapes for the API.

These are pure translation -- every field here comes directly from an
existing ``db.persistence``/``db.models`` value. No schema here decides
anything about task state; ``TaskResponse.from_record`` only reads
already-computed fields off a ``TaskRecord`` that ``db.persistence``
already produced.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from db.models import TaskRecord


class CreateWorkflowRequest(BaseModel):
    name: str


class WorkflowResponse(BaseModel):
    id: int
    name: str
    created_at: datetime


class CreateTaskRequest(BaseModel):
    name: str
    task_type: str
    max_attempts: int = 1
    payload: Optional[dict] = None
    depends_on_ids: Optional[list[int]] = None


class TaskResponse(BaseModel):
    """The diagnostic field set (id, workflow_id, name,
    task_type, state, attempt_count, max_attempts, lease_owner,
    lease_expires_at, created_at, updated_at), plus ``payload`` -- the
    one addition beyond that list, included so a client can see back
    exactly what it submitted at creation time. Flagged here explicitly
    since it wasn't in the originally enumerated field list."""

    id: int
    workflow_id: int
    name: str
    task_type: str
    state: str
    attempt_count: int
    max_attempts: int
    lease_owner: Optional[str]
    lease_expires_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    payload: Optional[dict]

    @classmethod
    def from_record(cls, record: TaskRecord) -> "TaskResponse":
        return cls(
            id=record.id,
            workflow_id=record.workflow_id,
            name=record.name,
            task_type=record.task_type,
            state=record.instance.state.value,
            attempt_count=record.instance.attempt_count,
            max_attempts=record.instance.max_attempts,
            lease_owner=record.instance.lease_owner,
            lease_expires_at=record.instance.lease_expires_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
            payload=record.payload,
        )


class WorkflowStatusResponse(BaseModel):
    """Caller-facing workflow completion/rollup view. Every count
    comes directly from ``db.persistence.get_workflow_task_state_counts``
    (itself a single PostgreSQL aggregation query) -- ``is_complete`` and
    ``has_failures`` are the only two fields derived here, and both are
    trivial functions of the counts already computed by that query, not
    new state-machine logic.

    ``is_complete``: every task has reached a terminal state (SUCCEEDED,
    FAILED_TERMINAL, or SKIPPED) -- equivalently, none remain PENDING,
    READY, or RUNNING. A workflow with zero tasks is deliberately NOT
    complete: nothing in the existing project conventions (core/
    transitions.py's ``create_task`` always produces at least one
    PENDING/READY task; there is no notion anywhere else of a workflow
    existing with no tasks yet being "done") suggests an empty workflow
    should read as finished, so this is the smallest defensible choice
    rather than the vacuously-true one.

    ``has_failures``: at least one task is FAILED_TERMINAL. SKIPPED tasks
    do NOT count as failures here -- they are a downstream consequence of
    a failure elsewhere in the DAG, not a failure themselves (see
    ``core.transitions.skip`` and the skip-cascade docstrings in
    db/persistence.py), so a workflow made up entirely of SKIPPED tasks is
    complete with ``has_failures=false``.
    """

    workflow_id: int
    total_tasks: int
    pending: int
    ready: int
    running: int
    succeeded: int
    failed_terminal: int
    skipped: int
    is_complete: bool
    has_failures: bool

    @classmethod
    def from_counts(cls, workflow_id: int, counts: dict) -> "WorkflowStatusResponse":
        active = counts["pending"] + counts["ready"] + counts["running"]
        return cls(
            workflow_id=workflow_id,
            total_tasks=counts["total_tasks"],
            pending=counts["pending"],
            ready=counts["ready"],
            running=counts["running"],
            succeeded=counts["succeeded"],
            failed_terminal=counts["failed_terminal"],
            skipped=counts["skipped"],
            is_complete=counts["total_tasks"] > 0 and active == 0,
            has_failures=counts["failed_terminal"] > 0,
        )
