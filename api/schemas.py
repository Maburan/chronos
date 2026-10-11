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

from pydantic import BaseModel, EmailStr, Field

from db.models import TaskRecord


class RegisterRequest(BaseModel):
    email: EmailStr
    # bcrypt (auth/passwords.py) hard-rejects any password whose UTF-8
    # encoding exceeds 72 bytes -- max_length=72 here (chars, a stricter
    # bound than bytes since a multi-byte character could still exceed 72
    # bytes at fewer than 72 chars) keeps that failure a clean 422 instead
    # of an unhandled 500 from bcrypt.hashpw. Found and fixed during the
    # Phase 8 security review; auth/passwords.py::hash_password also
    # checks byte length directly as defense in depth for any other
    # caller that doesn't go through this schema.
    password: str = Field(min_length=8, max_length=72)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: int
    email: str
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class TaskTypeFieldInfo(BaseModel):
    """Mirrors one entry of a TASK_TYPE_METADATA field list
    (worker/tasks/__init__.py) field-for-field. The presentation-only
    additions (label/help_text/placeholder/input_type/advanced/options/
    validation) are all optional so a field that omits them still
    validates -- the frontend's generic renderer falls back to sane
    defaults (e.g. input_type="text", advanced=False) rather than this
    schema forcing every field dict to spell them out."""

    name: str
    type: str
    required: bool
    default: Optional[object] = None
    label: Optional[str] = None
    help_text: Optional[str] = None
    placeholder: Optional[str] = None
    input_type: Optional[str] = None
    advanced: bool = False
    options: Optional[list[str]] = None
    validation: Optional[dict] = None


class TaskTypeInfo(BaseModel):
    type: str
    label: str
    description: str
    category: Optional[str] = None
    icon: Optional[str] = None
    fields: list[TaskTypeFieldInfo]


class CreateWorkflowRequest(BaseModel):
    name: str


class WorkflowResponse(BaseModel):
    id: int
    name: str
    user_id: int
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
    depends_on_ids: list[int]
    # None for a task created directly (no run involved, the only kind
    # that existed before the Definition/Run model) -- populated only for
    # tasks materialized by POST /workflows/{id}/runs. See
    # db/models.py::TaskRecord.
    workflow_run_id: Optional[int] = None
    task_definition_id: Optional[int] = None

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
            depends_on_ids=list(record.depends_on_ids),
            workflow_run_id=record.workflow_run_id,
            task_definition_id=record.task_definition_id,
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


# ---------------------------------------------------------------------------
# workflow definitions (Builder design-time graph -- Save)
# ---------------------------------------------------------------------------


class TaskDefinitionInput(BaseModel):
    """One Builder node in a Save request. ``client_id`` is caller-chosen
    (e.g. the frontend's in-memory node id) and only used to resolve
    ``TaskDefinitionDependencyInput`` edges within the same request -- see
    ``db.persistence.save_workflow_definition``."""

    client_id: str
    name: str
    task_type: str
    max_attempts: int = 1
    payload: Optional[dict] = None
    position_x: Optional[float] = None
    position_y: Optional[float] = None


class TaskDefinitionDependencyInput(BaseModel):
    task_client_id: str
    depends_on_client_id: str
    # Reserved for the isolated If/Else branching phase -- stored as-is,
    # not yet interpreted by run materialization (every edge is currently
    # a plain AND-dependency, exactly like task_dependencies today).
    branch: Optional[str] = None


class SaveWorkflowDefinitionRequest(BaseModel):
    tasks: list[TaskDefinitionInput]
    dependencies: list[TaskDefinitionDependencyInput] = Field(default_factory=list)


class TaskDefinitionResponse(BaseModel):
    id: int
    workflow_id: int
    name: str
    task_type: str
    max_attempts: int
    payload: Optional[dict]
    position_x: Optional[float]
    position_y: Optional[float]
    depends_on_ids: list[int]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: dict) -> "TaskDefinitionResponse":
        return cls(
            id=row["id"],
            workflow_id=row["workflow_id"],
            name=row["name"],
            task_type=row["task_type"],
            max_attempts=row["max_attempts"],
            payload=row["payload"],
            position_x=row["position_x"],
            position_y=row["position_y"],
            depends_on_ids=list(row["depends_on_ids"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class WorkflowDefinitionResponse(BaseModel):
    workflow_id: int
    tasks: list[TaskDefinitionResponse]


# ---------------------------------------------------------------------------
# workflow runs (materialized executions -- Run)
# ---------------------------------------------------------------------------


class WorkflowRunResponse(BaseModel):
    id: int
    workflow_id: int
    triggered_by_user_id: int
    created_at: datetime
    # The immutable record of exactly what this run executed -- see
    # db/migrations/0004_workflow_definitions_and_runs.sql and
    # db.persistence.create_workflow_run's docstring. Present here mainly
    # so a client (or a future audit) never has to reconstruct it from
    # task_definitions, which may have since changed or been deleted.
    definition_snapshot: dict

    @classmethod
    def from_row(cls, row: dict) -> "WorkflowRunResponse":
        return cls(
            id=row["id"],
            workflow_id=row["workflow_id"],
            triggered_by_user_id=row["triggered_by_user_id"],
            created_at=row["created_at"],
            definition_snapshot=row["definition_snapshot"],
        )


class RunStatusResponse(BaseModel):
    """The run-scoped equivalent of ``WorkflowStatusResponse`` -- same
    fields and the same ``is_complete``/``has_failures`` derivation, scoped
    to one run's materialized tasks instead of every task a workflow has
    ever had across every run."""

    run_id: int
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
    def from_counts(cls, run_id: int, counts: dict) -> "RunStatusResponse":
        active = counts["pending"] + counts["ready"] + counts["running"]
        return cls(
            run_id=run_id,
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
