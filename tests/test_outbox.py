"""Phase 2: transactional outbox tests.

Covers the 0002_outbox.sql schema itself, and the central invariant that
every task transition producing READY inserts exactly one TASK_READY
outbox row in the SAME PostgreSQL transaction as the state write -- never
committed separately, never left behind by a rolled-back transition.

Uses the same real-PostgreSQL fixtures as the rest of the suite (``db``,
``db_dsn``) -- no mocks for anything transactional or concurrent.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from core.transitions import TaskState
from db import persistence, queries
from db.connection import get_pool
from db.migrate import apply_migrations, applied_versions

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)


def _make_workflow(db):
    return persistence.create_workflow(db, name="wf", user_id=db.test_user_id)


def _outbox_rows_for_task(db, task_id: int) -> list[dict]:
    with db.connection() as conn:
        return conn.execute(
            "SELECT * FROM outbox_events WHERE task_id = %s ORDER BY id", (task_id,)
        ).fetchall()


def _outbox_count(db) -> int:
    with db.connection() as conn:
        return conn.execute("SELECT count(*) AS n FROM outbox_events").fetchone()["n"]


# ---------------------------------------------------------------------------
# A. schema
# ---------------------------------------------------------------------------


def test_0002_applies_cleanly_after_0001(pg_test_dsn):
    # Same order-independence rationale as test_db_schema.py's migration
    # test: don't assume this is the first consumer of the shared session
    # database, just assert the end state and idempotency.
    with psycopg.connect(pg_test_dsn) as conn:
        apply_migrations(conn)
        versions = applied_versions(conn)
        assert "0001_init" in versions
        assert "0002_outbox" in versions

        second_run = apply_migrations(conn)
    assert second_run == []


def test_outbox_events_table_exists(db):
    with db.connection() as conn:
        row = conn.execute(
            "SELECT to_regclass('public.outbox_events') AS reg"
        ).fetchone()
    assert row["reg"] == "outbox_events"


def test_outbox_id_is_bigint_identity(db):
    with db.connection() as conn:
        row = conn.execute(
            "SELECT data_type, is_identity FROM information_schema.columns "
            "WHERE table_name = 'outbox_events' AND column_name = 'id'"
        ).fetchone()
    assert row["data_type"] == "bigint"
    assert row["is_identity"] == "YES"


def test_outbox_task_id_is_bigint_fk_to_tasks(db):
    with db.connection() as conn:
        col = conn.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'outbox_events' AND column_name = 'task_id'"
        ).fetchone()
        assert col["data_type"] == "bigint"

        fk = conn.execute(
            """
            SELECT ccu.table_name AS referenced_table
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.constraint_column_usage ccu
                ON tc.constraint_name = ccu.constraint_name
            WHERE tc.table_name = 'outbox_events'
                AND tc.constraint_type = 'FOREIGN KEY'
                AND kcu.column_name = 'task_id'
            """
        ).fetchone()
    assert fk is not None
    assert fk["referenced_table"] == "tasks"


def test_outbox_workflow_id_is_bigint_fk_to_workflows(db):
    with db.connection() as conn:
        col = conn.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'outbox_events' AND column_name = 'workflow_id'"
        ).fetchone()
        assert col["data_type"] == "bigint"

        fk = conn.execute(
            """
            SELECT ccu.table_name AS referenced_table
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.constraint_column_usage ccu
                ON tc.constraint_name = ccu.constraint_name
            WHERE tc.table_name = 'outbox_events'
                AND tc.constraint_type = 'FOREIGN KEY'
                AND kcu.column_name = 'workflow_id'
            """
        ).fetchone()
    assert fk is not None
    assert fk["referenced_table"] == "workflows"


def test_outbox_event_type_not_null_defaults_to_task_ready(db):
    with db.connection() as conn:
        row = conn.execute(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'outbox_events' AND column_name = 'event_type'"
        ).fetchone()
    assert row["is_nullable"] == "NO"
    assert "TASK_READY" in row["column_default"]


def test_outbox_payload_is_jsonb_not_null(db):
    with db.connection() as conn:
        row = conn.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'outbox_events' AND column_name = 'payload'"
        ).fetchone()
    assert row["data_type"] == "jsonb"
    assert row["is_nullable"] == "NO"


def test_outbox_created_at_not_null_published_at_nullable(db):
    with db.connection() as conn:
        rows = {
            r["column_name"]: r["is_nullable"]
            for r in conn.execute(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'outbox_events' "
                "AND column_name IN ('created_at', 'published_at')"
            ).fetchall()
        }
    assert rows["created_at"] == "NO"
    assert rows["published_at"] == "YES"


def test_idx_outbox_unpublished_exists_and_is_partial(db):
    with db.connection() as conn:
        row = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_outbox_unpublished'"
        ).fetchone()
    assert row is not None
    assert "WHERE" in row["indexdef"]
    assert "published_at" in row["indexdef"]


# ---------------------------------------------------------------------------
# B. creation
# ---------------------------------------------------------------------------


def test_create_task_without_dependencies_creates_ready_task_and_one_outbox_row(db):
    wf = _make_workflow(db)
    record = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    assert record.instance.state == TaskState.READY

    rows = _outbox_rows_for_task(db, record.id)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "TASK_READY"
    assert rows[0]["workflow_id"] == wf["id"]
    assert rows[0]["published_at"] is None
    assert rows[0]["payload"] == {
        "task_id": record.id,
        "task_type": "noop",
        "workflow_id": wf["id"],
    }


def test_create_task_with_dependencies_creates_pending_task_and_zero_outbox_rows(db):
    wf = _make_workflow(db)
    dep = persistence.create_task(db, workflow_id=wf["id"], name="dep", task_type="noop")
    record = persistence.create_task(
        db, workflow_id=wf["id"], name="downstream", task_type="noop", depends_on_ids=[dep.id]
    )
    assert record.instance.state == TaskState.PENDING
    assert _outbox_rows_for_task(db, record.id) == []


# ---------------------------------------------------------------------------
# C. dependency propagation
# ---------------------------------------------------------------------------


def test_dependency_propagation_creates_exactly_one_outbox_row(db):
    wf = _make_workflow(db)
    a = persistence.create_task(db, workflow_id=wf["id"], name="a", task_type="noop")
    c = persistence.create_task(
        db, workflow_id=wf["id"], name="c", task_type="noop", depends_on_ids=[a.id]
    )
    # a's own creation already produced one outbox row; c's should not yet.
    assert _outbox_rows_for_task(db, c.id) == []

    claimed_a = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    persistence.report_task_success(db, task_id=claimed_a.id, worker_id="w1", now=NOW)

    rows = _outbox_rows_for_task(db, c.id)
    assert len(rows) == 1
    assert rows[0]["payload"]["task_id"] == c.id


def test_multiple_dependents_becoming_ready_create_one_row_each(db):
    wf = _make_workflow(db)
    a = persistence.create_task(db, workflow_id=wf["id"], name="a", task_type="noop")
    c1 = persistence.create_task(
        db, workflow_id=wf["id"], name="c1", task_type="noop", depends_on_ids=[a.id]
    )
    c2 = persistence.create_task(
        db, workflow_id=wf["id"], name="c2", task_type="noop", depends_on_ids=[a.id]
    )

    claimed_a = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    persistence.report_task_success(db, task_id=claimed_a.id, worker_id="w1", now=NOW)

    assert len(_outbox_rows_for_task(db, c1.id)) == 1
    assert len(_outbox_rows_for_task(db, c2.id)) == 1


def test_fan_in_dependency_completion_produces_no_premature_event(db):
    wf = _make_workflow(db)
    a = persistence.create_task(db, workflow_id=wf["id"], name="a", task_type="noop")
    b = persistence.create_task(db, workflow_id=wf["id"], name="b", task_type="noop")
    c = persistence.create_task(
        db, workflow_id=wf["id"], name="c", task_type="noop", depends_on_ids=[a.id, b.id]
    )

    claimed_a = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    persistence.report_task_success(db, task_id=claimed_a.id, worker_id="w1", now=NOW)
    assert _outbox_rows_for_task(db, c.id) == []  # b not done yet -- no event

    claimed_b = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    persistence.report_task_success(db, task_id=claimed_b.id, worker_id="w1", now=NOW)
    assert len(_outbox_rows_for_task(db, c.id)) == 1  # now READY -- exactly one event


def test_concurrent_fan_in_completion_does_not_create_duplicate_outbox_rows(db, db_dsn):
    wf = persistence.create_workflow(db, name="fan-in-outbox-wf", user_id=db.test_user_id)
    x = persistence.create_task(db, workflow_id=wf["id"], name="x", task_type="noop")
    y = persistence.create_task(db, workflow_id=wf["id"], name="y", task_type="noop")
    z = persistence.create_task(
        db, workflow_id=wf["id"], name="z", task_type="noop", depends_on_ids=[x.id, y.id]
    )

    def complete(task_id: int, worker_id: str):
        local_pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            claimed = persistence.claim_task(
                local_pool, worker_id=worker_id, now=NOW, lease_duration=LEASE
            )
            persistence.report_task_success(
                local_pool, task_id=claimed.id, worker_id=worker_id, now=NOW
            )
        finally:
            local_pool.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(complete, x.id, "w1"),
            executor.submit(complete, y.id, "w2"),
        ]
        for future in as_completed(futures):
            future.result()

    rows = _outbox_rows_for_task(db, z.id)
    assert len(rows) == 1  # never duplicated, never dropped, under real contention


# ---------------------------------------------------------------------------
# D. retry via report_failure
# ---------------------------------------------------------------------------


def test_retry_via_report_failure_creates_one_outbox_event(db):
    wf = _make_workflow(db)
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="noop", max_attempts=3
    )
    # creation itself already emitted one event -- track the baseline.
    before = len(_outbox_rows_for_task(db, task.id))

    claimed = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    result = persistence.report_task_failure(db, task_id=claimed.id, worker_id="w1", now=NOW)
    assert result.record.instance.state == TaskState.READY

    rows = _outbox_rows_for_task(db, task.id)
    assert len(rows) == before + 1
    assert rows[-1]["event_type"] == "TASK_READY"


def test_terminal_failure_creates_no_additional_outbox_event(db):
    wf = _make_workflow(db)
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="noop", max_attempts=1
    )
    before = len(_outbox_rows_for_task(db, task.id))

    claimed = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    result = persistence.report_task_failure(db, task_id=claimed.id, worker_id="w1", now=NOW)
    assert result.record.instance.state == TaskState.FAILED_TERMINAL

    assert len(_outbox_rows_for_task(db, task.id)) == before  # no new event


# ---------------------------------------------------------------------------
# E. retry via expire_lease
# ---------------------------------------------------------------------------


def test_retry_via_expire_lease_creates_one_outbox_event(db):
    wf = _make_workflow(db)
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="noop", max_attempts=3
    )
    before = len(_outbox_rows_for_task(db, task.id))

    claimed = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    after_expiry = NOW + LEASE + timedelta(seconds=1)
    result = persistence.expire_lease(db, task_id=claimed.id, now=after_expiry)
    assert result.record.instance.state == TaskState.READY

    rows = _outbox_rows_for_task(db, task.id)
    assert len(rows) == before + 1
    assert rows[-1]["event_type"] == "TASK_READY"


def test_terminal_expire_lease_creates_no_additional_outbox_event(db):
    wf = _make_workflow(db)
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="noop", max_attempts=1
    )
    before = len(_outbox_rows_for_task(db, task.id))

    claimed = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    after_expiry = NOW + LEASE + timedelta(seconds=1)
    result = persistence.expire_lease(db, task_id=claimed.id, now=after_expiry)
    assert result.record.instance.state == TaskState.FAILED_TERMINAL

    assert len(_outbox_rows_for_task(db, task.id)) == before


# ---------------------------------------------------------------------------
# F. non-READY transitions never emit
# ---------------------------------------------------------------------------


def test_claim_ready_to_running_creates_no_outbox_event(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    before = len(_outbox_rows_for_task(db, task.id))  # 1, from creation

    persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)

    assert len(_outbox_rows_for_task(db, task.id)) == before


def test_success_creates_no_outbox_event_for_the_succeeding_task_itself(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    before = len(_outbox_rows_for_task(db, task.id))

    claimed = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    persistence.report_task_success(db, task_id=claimed.id, worker_id="w1", now=NOW)

    assert len(_outbox_rows_for_task(db, task.id)) == before  # no event for itself


def test_skip_cascade_creates_no_outbox_events(db):
    wf = _make_workflow(db)
    a = persistence.create_task(
        db, workflow_id=wf["id"], name="a", task_type="noop", max_attempts=1
    )
    b = persistence.create_task(db, workflow_id=wf["id"], name="b", task_type="noop", depends_on_ids=[a.id])
    c = persistence.create_task(db, workflow_id=wf["id"], name="c", task_type="noop", depends_on_ids=[a.id])
    d = persistence.create_task(
        db, workflow_id=wf["id"], name="d", task_type="noop", depends_on_ids=[b.id, c.id]
    )

    claimed_a = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    result = persistence.report_task_failure(db, task_id=claimed_a.id, worker_id="w1", now=NOW)
    assert result.record.instance.state == TaskState.FAILED_TERMINAL

    for skipped_task in (b, c, d):
        assert _outbox_rows_for_task(db, skipped_task.id) == []


# ---------------------------------------------------------------------------
# G. transaction rollback
# ---------------------------------------------------------------------------


def test_rolled_back_ready_transition_leaves_no_outbox_row(db):
    wf = _make_workflow(db)
    with db.connection() as conn:
        with pytest.raises(psycopg.Error):
            with conn.transaction():
                cur = conn.cursor()
                cur.execute(
                    queries.INSERT_TASK,
                    {
                        "workflow_id": wf["id"],
                        "name": "will-roll-back",
                        "task_type": "noop",
                        "state": "READY",
                        "max_attempts": 1,
                        "payload": None,
                    },
                )
                row = cur.fetchone()
                task_id = row["id"]
                cur.execute(
                    queries.INSERT_OUTBOX_EVENT,
                    {
                        "task_id": task_id,
                        "workflow_id": wf["id"],
                        "payload": psycopg.types.json.Jsonb(
                            {"task_id": task_id, "task_type": "noop", "workflow_id": wf["id"]}
                        ),
                    },
                )
                # Force the whole transaction to roll back after both writes.
                cur.execute("SELECT 1 / 0")

    with db.connection() as conn:
        task_row = conn.execute(
            "SELECT count(*) AS n FROM tasks WHERE name = 'will-roll-back'"
        ).fetchone()
        outbox_row = conn.execute(
            "SELECT count(*) AS n FROM outbox_events oe "
            "JOIN tasks t ON t.id = oe.task_id WHERE t.name = 'will-roll-back'"
        ).fetchone()
    assert task_row["n"] == 0
    assert outbox_row["n"] == 0


def test_outbox_insert_failure_rolls_back_the_task_transition(db):
    wf = _make_workflow(db)
    with db.connection() as conn:
        with pytest.raises(psycopg.Error):
            with conn.transaction():
                cur = conn.cursor()
                cur.execute(
                    queries.INSERT_TASK,
                    {
                        "workflow_id": wf["id"],
                        "name": "outbox-fk-violation",
                        "task_type": "noop",
                        "state": "READY",
                        "max_attempts": 1,
                        "payload": None,
                    },
                )
                row = cur.fetchone()
                task_id = row["id"]
                # workflow_id references a non-existent workflow -> FK
                # violation on the outbox insert itself.
                cur.execute(
                    queries.INSERT_OUTBOX_EVENT,
                    {
                        "task_id": task_id,
                        "workflow_id": 999_999_999,
                        "payload": psycopg.types.json.Jsonb(
                            {"task_id": task_id, "task_type": "noop", "workflow_id": 999_999_999}
                        ),
                    },
                )

    with db.connection() as conn:
        task_row = conn.execute(
            "SELECT count(*) AS n FROM tasks WHERE name = 'outbox-fk-violation'"
        ).fetchone()
    assert task_row["n"] == 0  # the task insert rolled back too


def test_applied_false_transition_creates_no_outbox_event(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    before = len(_outbox_rows_for_task(db, task.id))

    claimed = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    # Stale/non-owning worker report -- applied=False, no state change at all.
    result = persistence.report_task_success(db, task_id=claimed.id, worker_id="intruder", now=NOW)
    assert result.applied is False

    assert len(_outbox_rows_for_task(db, task.id)) == before


# ---------------------------------------------------------------------------
# H. idempotency / race behavior
# ---------------------------------------------------------------------------


def test_concurrent_retries_preserve_one_event_per_ready_transition(db, db_dsn):
    wf = persistence.create_workflow(db, name="retry-outbox-wf", user_id=db.test_user_id)
    max_attempts = 3
    task = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="noop", max_attempts=max_attempts
    )
    baseline = len(_outbox_rows_for_task(db, task.id))  # 1, from creation

    def hammer(worker_id: str):
        local_pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            for _ in range(max_attempts + 5):
                claimed = persistence.claim_task(
                    local_pool, worker_id=worker_id, now=NOW, lease_duration=LEASE
                )
                if claimed is None:
                    continue
                persistence.report_task_failure(
                    local_pool, task_id=claimed.id, worker_id=worker_id, now=NOW
                )
        finally:
            local_pool.close()

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(hammer, f"w{i}") for i in range(5)]
        for future in as_completed(futures):
            future.result()

    with db.connection() as conn:
        row = conn.execute(
            "SELECT state, attempt_count FROM tasks WHERE id = %s", (task.id,)
        ).fetchone()
    assert row["state"] == "FAILED_TERMINAL"
    assert row["attempt_count"] == max_attempts

    # Exactly one READY-producing retry per attempt beyond the first
    # (attempt 1 claims the task created READY; each subsequent retry is
    # itself one more READY transition), plus the one from creation --
    # never more, never fewer, regardless of thread interleaving.
    rows = _outbox_rows_for_task(db, task.id)
    assert len(rows) == baseline + (max_attempts - 1)
    assert all(r["event_type"] == "TASK_READY" for r in rows)


def test_no_double_claim_implies_no_duplicate_creation_events(db, db_dsn):
    # Every task created here starts READY (zero deps) -- exactly one
    # outbox row per task must exist regardless of concurrent claiming
    # racing against the (already-committed, single-transaction) creation.
    wf = persistence.create_workflow(db, name="no-dup-outbox-wf", user_id=db.test_user_id)
    n_tasks = 20
    ids = [
        persistence.create_task(db, workflow_id=wf["id"], name=f"t{i}", task_type="noop").id
        for i in range(n_tasks)
    ]

    def worker_loop(worker_id: str):
        local_pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            while True:
                record = persistence.claim_task(
                    local_pool, worker_id=worker_id, now=NOW, lease_duration=LEASE
                )
                if record is None:
                    break
        finally:
            local_pool.close()

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(worker_loop, f"w{i}") for i in range(5)]
        for future in as_completed(futures):
            future.result()

    with db.connection() as conn:
        rows = conn.execute(
            "SELECT task_id, count(*) AS n FROM outbox_events "
            "WHERE task_id = ANY(%s) GROUP BY task_id",
            (ids,),
        ).fetchall()
    assert len(rows) == n_tasks
    assert all(r["n"] == 1 for r in rows)
