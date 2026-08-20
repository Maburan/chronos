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
"""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from psycopg_pool import ConnectionPool

from api.schemas import (
    CreateTaskRequest,
    CreateWorkflowRequest,
    TaskResponse,
    WorkflowResponse,
    WorkflowStatusResponse,
)
from db import persistence

router = APIRouter()


def get_pool(request: Request) -> ConnectionPool:
    """The connection pool the app's lifespan (see api/app.py) created at
    startup. A plain function (not a class) so tests can override it via
    FastAPI's ``dependency_overrides`` with a test pool, exactly the same
    override pattern used for every other injected dependency in this
    project's tests (e.g. ``worker/task_worker.py``'s injected ``clock``).
    """
    return request.app.state.pool


@router.post(
    "/workflows", response_model=WorkflowResponse, status_code=status.HTTP_201_CREATED
)
def create_workflow(
    body: CreateWorkflowRequest, pool: ConnectionPool = Depends(get_pool)
) -> WorkflowResponse:
    row = persistence.create_workflow(pool, name=body.name)
    return WorkflowResponse(**row)


@router.get("/workflows/{workflow_id}", response_model=WorkflowResponse)
def get_workflow(
    workflow_id: int, pool: ConnectionPool = Depends(get_pool)
) -> WorkflowResponse:
    row = persistence.get_workflow(pool, workflow_id=workflow_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
    return WorkflowResponse(**row)


@router.post(
    "/workflows/{workflow_id}/tasks",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_task(
    workflow_id: int, body: CreateTaskRequest, pool: ConnectionPool = Depends(get_pool)
) -> TaskResponse:
    # Existence pre-check via the existing get_workflow read (not a new
    # mutation, not SQL written here) so a nonexistent workflow_id returns
    # a clean 404 rather than surfacing as an uncaught FK violation from
    # the insert below.
    if persistence.get_workflow(pool, workflow_id=workflow_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")

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
def get_task(task_id: int, pool: ConnectionPool = Depends(get_pool)) -> TaskResponse:
    record = persistence.get_task(pool, task_id=task_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    return TaskResponse.from_record(record)


@router.get("/workflows/{workflow_id}/tasks", response_model=list[TaskResponse])
def list_workflow_tasks(
    workflow_id: int, pool: ConnectionPool = Depends(get_pool)
) -> list[TaskResponse]:
    if persistence.get_workflow(pool, workflow_id=workflow_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
    records = persistence.list_tasks_for_workflow(pool, workflow_id=workflow_id)
    return [TaskResponse.from_record(r) for r in records]


@router.get("/workflows/{workflow_id}/status", response_model=WorkflowStatusResponse)
def get_workflow_status(
    workflow_id: int, pool: ConnectionPool = Depends(get_pool)
) -> WorkflowStatusResponse:
    # Same existence pre-check pattern as list_workflow_tasks/create_task
    # above: a plain, unlocked get_workflow read, translated to a 404
    # before any aggregation query runs.
    if persistence.get_workflow(pool, workflow_id=workflow_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
    counts = persistence.get_workflow_task_state_counts(pool, workflow_id=workflow_id)
    return WorkflowStatusResponse.from_counts(workflow_id, counts)
