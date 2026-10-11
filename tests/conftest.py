"""Shared fixtures for the Phase 1 PostgreSQL integration/concurrency tests.

Resolution order for a test database, per the approved brief:

  1. ``CHRONOS_TEST_DATABASE_URL`` env var (CI's job: point this at an
     admin-capable Postgres).
  2. A ``testcontainers``-managed ephemeral Postgres, for local dev
     convenience.
  3. Neither available -> skip the whole DB integration suite cleanly.
     ``tests/test_transitions.py`` has zero DB dependency and keeps
     running regardless.

A unique database is created per test *session* (not per test), migrated
once, and truncated before each test function -- concurrency tests need
genuinely independent, separately-committed connections, so per-test
isolation can't rely on an outer transaction rollback.
"""

from __future__ import annotations

import os
import uuid
from typing import Iterator

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from db.connection import get_pool
from db.migrate import apply_migrations


def _with_dbname(dsn: str, dbname: str) -> str:
    parts = conninfo_to_dict(dsn)
    parts["dbname"] = dbname
    return make_conninfo(**parts)


@pytest.fixture(scope="session")
def pg_test_dsn() -> Iterator[str]:
    admin_dsn = os.environ.get("CHRONOS_TEST_DATABASE_URL")
    container = None

    if not admin_dsn:
        try:
            from testcontainers.postgres import PostgresContainer
        except ImportError:
            pytest.skip(
                "no CHRONOS_TEST_DATABASE_URL and testcontainers is not "
                "installed; skipping DB integration tests"
            )

        try:
            container = PostgresContainer("postgres:16-alpine")
            container.start()
        except Exception as exc:  # noqa: BLE001 - any container startup failure means "skip"
            pytest.skip(
                f"no Postgres available (testcontainers failed to start: {exc}); "
                "skipping DB integration tests"
            )

        admin_dsn = container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")

    admin_root_dsn = _with_dbname(admin_dsn, "postgres")
    test_db_name = f"chronos_test_{uuid.uuid4().hex[:16]}"

    try:
        with psycopg.connect(admin_root_dsn, autocommit=True) as admin_conn:
            admin_conn.execute(f'CREATE DATABASE "{test_db_name}"')
    except psycopg.OperationalError as exc:
        if container is not None:
            container.stop()
        pytest.skip(f"could not reach Postgres to create a test database: {exc}")

    test_dsn = _with_dbname(admin_dsn, test_db_name)

    try:
        yield test_dsn
    finally:
        with psycopg.connect(admin_root_dsn, autocommit=True) as admin_conn:
            admin_conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (test_db_name,),
            )
            admin_conn.execute(f'DROP DATABASE IF EXISTS "{test_db_name}"')
        if container is not None:
            container.stop()


@pytest.fixture(scope="session")
def pg_pool(pg_test_dsn: str):
    with psycopg.connect(pg_test_dsn) as conn:
        apply_migrations(conn)

    pool = get_pool(pg_test_dsn, min_size=2, max_size=20)
    yield pool
    pool.close()


@pytest.fixture()
def db(pg_pool):
    """A pool bound to a freshly truncated database, with one bootstrap
    user row already present so tests that call
    ``persistence.create_workflow`` directly (claiming, outbox, reaper,
    heartbeat, fallback-poller, worker tests -- anything that isn't about
    auth/ownership itself) always have a valid owner to satisfy the
    ``workflows.user_id`` FK, without every such test needing its own
    auth setup. Call sites do
    ``persistence.create_workflow(db, name=..., user_id=db.test_user_id)``
    -- the id is stashed as a plain attribute on the returned pool object
    (it's the same object every test function in this file gets back, so
    this is just a convenient way to hand a second piece of fixture state
    back through the same ``db`` parameter every helper already takes)."""
    with pg_pool.connection() as conn:
        conn.execute(
            "TRUNCATE users, workflows, tasks, task_dependencies, "
            "task_definitions, task_definition_dependencies, workflow_runs "
            "RESTART IDENTITY CASCADE"
        )
        row = conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
            ("bootstrap-test-user@example.com", "not-a-real-bcrypt-hash"),
        ).fetchone()
        conn.commit()
    pg_pool.test_user_id = row["id"]
    return pg_pool


@pytest.fixture()
def db_dsn(pg_test_dsn, db):
    """The raw DSN, for tests that need to open their own independent
    connections (concurrency tests) rather than sharing the pool."""
    return pg_test_dsn


TEST_JWT_SECRET = "test-only-jwt-secret-not-for-production-use"


@pytest.fixture()
def api_app(db_dsn):
    """The bare FastAPI app (no default auth header) -- used by tests that
    need to construct their own users/tokens (auth/authorization tests).
    Most tests should use ``api_client`` instead."""
    from fastapi.testclient import TestClient

    from api.app import create_app
    from api.config import ApiConfig

    app = create_app(
        config=ApiConfig(
            database_url=db_dsn,
            pool_min_size=1,
            pool_max_size=5,
            jwt_secret=TEST_JWT_SECRET,
        )
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def api_client(api_app):
    """A FastAPI TestClient for the API, backed by its own real connection
    pool pointed at the same (already-migrated, freshly truncated via the
    ``db`` fixture) test database -- not a mock, not a dependency
    override, the app manages its pool exactly as it would in production.

    Pre-authenticated as one fixed test user (registered+logged in here,
    token set as the default Authorization header) so every pre-existing
    workflow/task/status test -- none of which is about auth itself --
    keeps exercising the same business logic unchanged now that every
    route requires a caller identity. Tests of auth/authorization
    themselves (tests/test_auth.py, tests/test_api_authorization.py) use
    the unauthenticated ``api_app`` fixture directly instead."""
    register_resp = api_app.post(
        "/auth/register", json={"email": "fixture-user@example.com", "password": "hunter22"}
    )
    assert register_resp.status_code == 201, register_resp.text
    login_resp = api_app.post(
        "/auth/login", json={"email": "fixture-user@example.com", "password": "hunter22"}
    )
    assert login_resp.status_code == 200, login_resp.text
    token = login_resp.json()["access_token"]
    api_app.headers["Authorization"] = f"Bearer {token}"
    return api_app
