"""API routes: a thin front door onto the existing, unmodified
``db.persistence`` layer.

    HTTP request
        |
        v
    api/routes.py (this module)   <-- translation only: request body ->
        |                             persistence call -> response shape.
        | db.persistence.*             No SQL, no state-transition logic,
        v                              no direct task-state manipulation
    db/persistence.py                  anywhere in this file.
        |
        v
    PostgreSQL

Every mutation goes through an existing, unmodified persistence function
(``create_workflow``, ``create_task``) -- exactly the same functions
every other caller (tests, a future worker, direct Python use) already
uses. A task created here that has zero dependencies becomes READY and
gets exactly one outbox row through the exact same transactional-outbox
path every other caller already guarantees; the relay/RabbitMQ/worker/
reaper/Redis-limiter pipeline is
completely unaware a task originated from an HTTP request rather than a
direct call. This route module never talks to RabbitMQ or Redis, never
implements retries/leases/heartbeats/claiming/outbox behavior/dependency
propagation -- all of that remains entirely inside the layers this module
only ever calls into.

Every route below requires authentication (``Depends(get_current_user)``)
and, for anything scoped to a specific workflow, an ownership check
(``_require_owned_workflow``) -- a workflow belonging to a different user
returns 404, not 403, so a caller cannot even confirm the id exists.
"""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from psycopg_pool import ConnectionPool

from api.auth_dependency import AuthenticatedUser, get_current_user
from api.schemas import (
    CreateTaskRequest,
    CreateWorkflowRequest,
    RunStatusResponse,
    SaveWorkflowDefinitionRequest,
    TaskDefinitionResponse,
    TaskResponse,
    TaskTypeInfo,
    WorkflowDefinitionResponse,
    WorkflowResponse,
    WorkflowRunResponse,
    WorkflowStatusResponse,
)
from db import persistence
from worker.tasks import TASK_TYPE_METADATA

router = APIRouter()


@router.get("/task-types", response_model=list[TaskTypeInfo])
def list_task_types() -> list[TaskTypeInfo]:
    """Static, unauthenticated metadata describing every task type a
    worker can execute (see worker/tasks/__init__.py's TASK_TYPE_METADATA
    -- the single source of truth this route only reads from, never
    duplicates). No auth required: this is fixed server configuration,
    not user data, and the frontend's workflow-creation form needs it to
    render a task-type picker."""
    return [
        TaskTypeInfo(type=task_type, **info) for task_type, info in TASK_TYPE_METADATA.items()
    ]


def get_pool(request: Request) -> ConnectionPool:
    """The connection pool the app's lifespan (see api/app.py) created at
    startup. A plain function (not a class) so tests can override it via
    FastAPI's ``dependency_overrides`` with a test pool, exactly the same
    override pattern used for every other injected dependency in this
    project's tests (e.g. ``worker/task_worker.py``'s injected ``clock``).
    """
    return request.app.state.pool


def _require_owned_workflow(pool: ConnectionPool, *, workflow_id: int, user_id: int) -> dict:
    """Loads a workflow and verifies it belongs to ``user_id``. Returns
    404 (never 403) for both "does not exist" and "belongs to someone
    else" -- a caller must not be able to distinguish the two, or the
    existence of another user's workflow id leaks through the error code
    alone."""
    row = persistence.get_workflow(pool, workflow_id=workflow_id)
    if row is None or row["user_id"] != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
    return row


@router.post(
    "/workflows", response_model=WorkflowResponse, status_code=status.HTTP_201_CREATED
)
def create_workflow(
    body: CreateWorkflowRequest,
    pool: ConnectionPool = Depends(get_pool),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WorkflowResponse:
    row = persistence.create_workflow(pool, name=body.name, user_id=current_user.id)
    return WorkflowResponse(**row)


@router.get("/workflows", response_model=list[WorkflowResponse])
def list_workflows(
    pool: ConnectionPool = Depends(get_pool),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> list[WorkflowResponse]:
    rows = persistence.list_workflows_for_user(pool, user_id=current_user.id)
    return [WorkflowResponse(**row) for row in rows]


@router.get("/workflows/{workflow_id}", response_model=WorkflowResponse)
def get_workflow(
    workflow_id: int,
    pool: ConnectionPool = Depends(get_pool),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WorkflowResponse:
    row = _require_owned_workflow(pool, workflow_id=workflow_id, user_id=current_user.id)
    return WorkflowResponse(**row)


@router.post(
    "/workflows/{workflow_id}/tasks",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_task(
    workflow_id: int,
    body: CreateTaskRequest,
    pool: ConnectionPool = Depends(get_pool),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> TaskResponse:
    _require_owned_workflow(pool, workflow_id=workflow_id, user_id=current_user.id)

    try:
        record = persistence.create_task(
            pool,
            workflow_id=workflow_id,
            name=body.name,
            task_type=body.task_type,
            max_attempts=body.max_attempts,
            payload=body.payload,
            depends_on_ids=body.depends_on_ids,
        )
    except psycopg.errors.ForeignKeyViolation:
        # An invalid depends_on_ids entry. create_task's own transaction
        # has already rolled back completely (existing, unmodified
        # behavior -- see db/persistence.py's create_task docstring and
        # tests/test_persistence.py::test_create_task_rolls_back_entirely_on_bad_dependency).
        # This route only translates that outcome into a 400; it does not
        # change what rolled back or how.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid depends_on_ids: one or more referenced tasks do not exist",
        )

    return TaskResponse.from_record(record)


@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: int,
    pool: ConnectionPool = Depends(get_pool),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> TaskResponse:
    record = persistence.get_task(pool, task_id=task_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    _require_owned_workflow(
        pool, workflow_id=record.workflow_id, user_id=current_user.id
    )
    return TaskResponse.from_record(record)


@router.get("/workflows/{workflow_id}/tasks", response_model=list[TaskResponse])
def list_workflow_tasks(
    workflow_id: int,
    pool: ConnectionPool = Depends(get_pool),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> list[TaskResponse]:
    _require_owned_workflow(pool, workflow_id=workflow_id, user_id=current_user.id)
    records = persistence.list_tasks_for_workflow(pool, workflow_id=workflow_id)
    return [TaskResponse.from_record(r) for r in records]


@router.get("/workflows/{workflow_id}/status", response_model=WorkflowStatusResponse)
def get_workflow_status(
    workflow_id: int,
    pool: ConnectionPool = Depends(get_pool),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WorkflowStatusResponse:
    _require_owned_workflow(pool, workflow_id=workflow_id, user_id=current_user.id)
    counts = persistence.get_workflow_task_state_counts(pool, workflow_id=workflow_id)
    return WorkflowStatusResponse.from_counts(workflow_id, counts)


# ---------------------------------------------------------------------------
# workflow definitions (Builder design-time graph -- Save/load, never
# executed directly; see db.persistence.save_workflow_definition and
# db/migrations/0004_workflow_definitions_and_runs.sql)
# ---------------------------------------------------------------------------


@router.put("/workflows/{workflow_id}/definition", response_model=WorkflowDefinitionResponse)
def save_workflow_definition(
    workflow_id: int,
    body: SaveWorkflowDefinitionRequest,
    pool: ConnectionPool = Depends(get_pool),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WorkflowDefinitionResponse:
    _require_owned_workflow(pool, workflow_id=workflow_id, user_id=current_user.id)

    try:
        rows = persistence.save_workflow_definition(
            pool,
            workflow_id=workflow_id,
            tasks=[t.model_dump() for t in body.tasks],
            dependencies=[d.model_dump() for d in body.dependencies],
        )
    except (persistence.CyclicDependencyError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return WorkflowDefinitionResponse(
        workflow_id=workflow_id,
        tasks=[TaskDefinitionResponse.from_row(row) for row in rows],
    )


@router.get("/workflows/{workflow_id}/definition", response_model=WorkflowDefinitionResponse)
def get_workflow_definition(
    workflow_id: int,
    pool: ConnectionPool = Depends(get_pool),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WorkflowDefinitionResponse:
    _require_owned_workflow(pool, workflow_id=workflow_id, user_id=current_user.id)
    rows = persistence.get_workflow_definition(pool, workflow_id=workflow_id)
    return WorkflowDefinitionResponse(
        workflow_id=workflow_id,
        tasks=[TaskDefinitionResponse.from_row(row) for row in rows],
    )


# ---------------------------------------------------------------------------
# workflow runs (materialized executions -- Run; see
# db.persistence.create_workflow_run for the atomicity/immutability
# guarantees)
# ---------------------------------------------------------------------------


def _require_owned_run(pool: ConnectionPool, *, run_id: int, user_id: int) -> dict:
    """Loads a run and verifies its parent workflow belongs to ``user_id``.
    404 (never 403) for both "no such run" and "belongs to someone else",
    mirroring ``_require_owned_workflow``'s own rationale exactly."""
    run = persistence.get_workflow_run(pool, run_id=run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    _require_owned_workflow(pool, workflow_id=run["workflow_id"], user_id=user_id)
    return run


@router.post(
    "/workflows/{workflow_id}/runs",
    response_model=WorkflowRunResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_workflow_run(
    workflow_id: int,
    pool: ConnectionPool = Depends(get_pool),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WorkflowRunResponse:
    """The real Run action: materializes the workflow's current definition
    into a fresh, executable set of ``tasks``/``task_dependencies`` rows
    (see ``db.persistence.create_workflow_run``'s atomicity guarantee).
    This is the only route that ever creates dispatchable task rows from a
    definition -- ``PUT .../definition`` (Save) never does."""
    _require_owned_workflow(pool, workflow_id=workflow_id, user_id=current_user.id)

    try:
        run_row = persistence.create_workflow_run(
            pool, workflow_id=workflow_id, triggered_by_user_id=current_user.id
        )
    except persistence.EmptyWorkflowDefinitionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return WorkflowRunResponse.from_row(run_row)


@router.get("/workflows/{workflow_id}/runs", response_model=list[WorkflowRunResponse])
def list_workflow_runs(
    workflow_id: int,
    pool: ConnectionPool = Depends(get_pool),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> list[WorkflowRunResponse]:
    _require_owned_workflow(pool, workflow_id=workflow_id, user_id=current_user.id)
    rows = persistence.list_workflow_runs(pool, workflow_id=workflow_id)
    return [WorkflowRunResponse.from_row(row) for row in rows]


@router.get("/workflows/{workflow_id}/runs/{run_id}", response_model=WorkflowRunResponse)
def get_workflow_run(
    workflow_id: int,
    run_id: int,
    pool: ConnectionPool = Depends(get_pool),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> WorkflowRunResponse:
    run = _require_owned_run(pool, run_id=run_id, user_id=current_user.id)
    if run["workflow_id"] != workflow_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return WorkflowRunResponse.from_row(run)


@router.get(
    "/workflows/{workflow_id}/runs/{run_id}/tasks", response_model=list[TaskResponse]
)
def list_run_tasks(
    workflow_id: int,
    run_id: int,
    pool: ConnectionPool = Depends(get_pool),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> list[TaskResponse]:
    run = _require_owned_run(pool, run_id=run_id, user_id=current_user.id)
    if run["workflow_id"] != workflow_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    records = persistence.list_tasks_for_run(pool, run_id=run_id)
    return [TaskResponse.from_record(r) for r in records]


@router.get(
    "/workflows/{workflow_id}/runs/{run_id}/status", response_model=RunStatusResponse
)
def get_run_status(
    workflow_id: int,
    run_id: int,
    pool: ConnectionPool = Depends(get_pool),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> RunStatusResponse:
    run = _require_owned_run(pool, run_id=run_id, user_id=current_user.id)
    if run["workflow_id"] != workflow_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    counts = persistence.get_run_task_state_counts(pool, run_id=run_id)
    return RunStatusResponse.from_counts(run_id, counts)
