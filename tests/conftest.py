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
    """A pool bound to a freshly truncated database -- use this in tests
    that just need clean state on the shared pool."""
    with pg_pool.connection() as conn:
        conn.execute("TRUNCATE workflows, tasks, task_dependencies RESTART IDENTITY CASCADE")
        conn.commit()
    return pg_pool


@pytest.fixture()
def db_dsn(pg_test_dsn, db):
    """The raw DSN, for tests that need to open their own independent
    connections (concurrency tests) rather than sharing the pool."""
    return pg_test_dsn


@pytest.fixture()
def api_client(db_dsn):
    """A FastAPI TestClient for the Phase 8 API, backed by its own real
    connection pool pointed at the same (already-migrated, freshly
    truncated via the ``db`` fixture) test database -- not a mock, not a
    dependency override, the app manages its pool exactly as it would in
    production."""
    from fastapi.testclient import TestClient

    from api.app import create_app
    from api.config import ApiConfig

    app = create_app(
        config=ApiConfig(database_url=db_dsn, pool_min_size=1, pool_max_size=5)
    )
    with TestClient(app) as client:
        yield client
