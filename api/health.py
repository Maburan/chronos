"""Operational health/readiness endpoints -- deliberately separate from
api/routes.py's domain endpoints (workflows/tasks).

``/healthz`` is liveness: the process is up and answering HTTP at all,
nothing more. ``/readyz`` is readiness: the process can actually do its
job, checked here by proving PostgreSQL connectivity (the API's only
dependency -- it never talks to RabbitMQ or Redis). Both route handlers
call directly into ``db.persistence`` (``check_connection``), same "no
SQL in routes" rule as every other endpoint in this API.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from psycopg_pool import ConnectionPool

from api.routes import get_pool
from db import persistence

health_router = APIRouter()


@health_router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@health_router.get("/readyz")
def readyz(response: Response, pool: ConnectionPool = Depends(get_pool)) -> dict:
    try:
        persistence.check_connection(pool)
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "detail": str(exc)}
    return {"status": "ok"}
