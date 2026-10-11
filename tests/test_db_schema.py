"""Schema-level tests: migration idempotency, constraints, indexes."""

from __future__ import annotations

import psycopg
import pytest

from db.migrate import apply_migrations, applied_versions


def test_migration_applies_cleanly_and_is_idempotent(pg_test_dsn):
    # `pg_test_dsn` is a session-scoped database that other fixtures (e.g.
    # `pg_pool`, used by tests in other files) may already have migrated
    # by the time this test runs -- pytest's collection order is alphabetical
    # by filename, not declaration order, so this test cannot assume it's
    # the first thing to touch the database. Assert the actual invariants
    # -- "0001_init" ends up applied, and re-applying is a clean no-op --
    # rather than asserting this particular call is the one that applies it.
    with psycopg.connect(pg_test_dsn) as conn:
        apply_migrations(conn)
        assert "0001_init" in applied_versions(conn)

        second_run = apply_migrations(conn)

    assert second_run == []  # nothing left to apply, no error


def test_lease_pair_consistent_rejects_owner_without_expiry(db):
    with db.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO workflows (name, user_id) VALUES ('wf', %s) RETURNING id",
                    (db.test_user_id,),
                )
                wf_id = conn.execute("SELECT id FROM workflows ORDER BY id DESC LIMIT 1").fetchone()["id"]
                conn.execute(
                    "INSERT INTO tasks (workflow_id, name, task_type, state, lease_owner) "
                    "VALUES (%s, 't', 'noop', 'RUNNING', 'w1')",
                    (wf_id,),
                )


def test_lease_requires_running_rejects_lease_on_ready_task(db):
    with db.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO workflows (name, user_id) VALUES ('wf', %s)", (db.test_user_id,)
                )
                wf_id = conn.execute("SELECT id FROM workflows ORDER BY id DESC LIMIT 1").fetchone()["id"]
                conn.execute(
                    "INSERT INTO tasks (workflow_id, name, task_type, state, lease_owner, lease_expires_at) "
                    "VALUES (%s, 't', 'noop', 'READY', 'w1', now())",
                    (wf_id,),
                )


def test_attempt_bounds_rejects_attempt_count_over_max(db):
    with db.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO workflows (name, user_id) VALUES ('wf', %s)", (db.test_user_id,)
                )
                wf_id = conn.execute("SELECT id FROM workflows ORDER BY id DESC LIMIT 1").fetchone()["id"]
                conn.execute(
                    "INSERT INTO tasks (workflow_id, name, task_type, state, attempt_count, max_attempts) "
                    "VALUES (%s, 't', 'noop', 'READY', 2, 1)",
                    (wf_id,),
                )


def test_max_attempts_positive_rejects_zero(db):
    with db.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO workflows (name, user_id) VALUES ('wf', %s)", (db.test_user_id,)
                )
                wf_id = conn.execute("SELECT id FROM workflows ORDER BY id DESC LIMIT 1").fetchone()["id"]
                conn.execute(
                    "INSERT INTO tasks (workflow_id, name, task_type, state, max_attempts) "
                    "VALUES (%s, 't', 'noop', 'READY', 0)",
                    (wf_id,),
                )


def test_no_self_dependency_rejects_self_edge(db):
    with db.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO workflows (name, user_id) VALUES ('wf', %s)", (db.test_user_id,)
                )
                wf_id = conn.execute("SELECT id FROM workflows ORDER BY id DESC LIMIT 1").fetchone()["id"]
                conn.execute(
                    "INSERT INTO tasks (workflow_id, name, task_type, state) "
                    "VALUES (%s, 't', 'noop', 'READY')",
                    (wf_id,),
                )
                task_id = conn.execute("SELECT id FROM tasks ORDER BY id DESC LIMIT 1").fetchone()["id"]
                conn.execute(
                    "INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (%s, %s)",
                    (task_id, task_id),
                )


def test_required_indexes_exist(db):
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
        ).fetchall()
    names = {row["indexname"] for row in rows}

    for expected in (
        "idx_tasks_ready_claim",
        "idx_tasks_running_lease",
        "idx_tasks_workflow_id",
        "idx_tasks_state",
        "idx_task_deps_depends_on",
    ):
        assert expected in names


def test_ready_claim_index_is_partial(db):
    with db.connection() as conn:
        row = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_tasks_ready_claim'"
        ).fetchone()
    assert "WHERE" in row["indexdef"]
    assert "READY" in row["indexdef"]


def test_running_lease_index_is_partial(db):
    with db.connection() as conn:
        row = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_tasks_running_lease'"
        ).fetchone()
    assert "WHERE" in row["indexdef"]
    assert "RUNNING" in row["indexdef"]
