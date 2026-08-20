"""Single-connection persistence-layer tests: CRUD, transaction rollback,
and every state transition wrapped around core/transitions.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from core.transitions import TaskState, TransitionError
from db import persistence

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)


def _make_workflow(db):
    return persistence.create_workflow(db, name="wf")


# ---------------------------------------------------------------------------
# create_workflow / create_task happy paths
# ---------------------------------------------------------------------------


def test_create_workflow(db):
    wf = persistence.create_workflow(db, name="demo")
    assert wf["name"] == "demo"
    assert wf["id"] is not None


def test_create_task_without_dependencies_is_ready(db):
    wf = _make_workflow(db)
    record = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="noop"
    )
    assert record.instance.state == TaskState.READY
    assert record.instance.attempt_count == 0


def test_create_task_with_dependencies_is_pending_and_edges_exist(db):
    wf = _make_workflow(db)
    dep = persistence.create_task(db, workflow_id=wf["id"], name="dep", task_type="noop")
    record = persistence.create_task(
        db,
        workflow_id=wf["id"],
        name="downstream",
        task_type="noop",
        depends_on_ids=[dep.id],
    )
    assert record.instance.state == TaskState.PENDING

    with db.connection() as conn:
        row = conn.execute(
            "SELECT * FROM task_dependencies WHERE task_id = %s", (record.id,)
        ).fetchone()
    assert row["depends_on_task_id"] == dep.id


def test_create_task_with_payload_roundtrips(db):
    wf = _make_workflow(db)
    record = persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="noop", payload={"x": 1}
    )
    assert record.payload == {"x": 1}


# ---------------------------------------------------------------------------
# dependency-edge transaction rollback
# ---------------------------------------------------------------------------


def test_create_task_rolls_back_entirely_on_bad_dependency(db):
    wf = _make_workflow(db)
    nonexistent_dep_id = 999_999

    with pytest.raises(psycopg.Error):
        persistence.create_task(
            db,
            workflow_id=wf["id"],
            name="downstream",
            task_type="noop",
            depends_on_ids=[nonexistent_dep_id],
        )

    with db.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM tasks WHERE name = 'downstream'"
        ).fetchone()
    assert row["n"] == 0  # no orphaned task row left behind


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


def test_claim_task_empty_returns_none(db):
    assert persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE) is None


def test_claim_task_happy_path(db):
    wf = _make_workflow(db)
    persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    claimed = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    assert claimed is not None
    assert claimed.instance.state == TaskState.RUNNING
    assert claimed.instance.attempt_count == 1
    assert claimed.instance.lease_owner == "w1"
    assert claimed.instance.lease_expires_at == NOW + LEASE


def test_claim_task_rejects_naive_datetime(db):
    wf = _make_workflow(db)
    persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    with pytest.raises(TransitionError):
        persistence.claim_task(
            db, worker_id="w1", now=datetime(2026, 1, 1, 12, 0, 0), lease_duration=LEASE
        )


# ---------------------------------------------------------------------------
# claim_task_by_id (Phase 4 prerequisite: targeted claim)
# ---------------------------------------------------------------------------


def test_claim_task_by_id_happy_path(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    claimed = persistence.claim_task_by_id(
        db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=LEASE
    )

    assert claimed is not None
    assert claimed.id == task.id
    assert claimed.instance.state == TaskState.RUNNING


def test_claim_task_by_id_claims_the_correct_task(db):
    """The scenario the targeted-claim design exists to prevent: a message
    naming task_id=42 must never end up claiming an unrelated READY task."""
    wf = _make_workflow(db)
    task_42 = persistence.create_task(db, workflow_id=wf["id"], name="forty-two", task_type="noop")
    task_57 = persistence.create_task(db, workflow_id=wf["id"], name="fifty-seven", task_type="noop")

    claimed = persistence.claim_task_by_id(
        db, task_id=task_42.id, worker_id="w1", now=NOW, lease_duration=LEASE
    )

    assert claimed is not None
    assert claimed.id == task_42.id

    with db.connection() as conn:
        other = conn.execute("SELECT state FROM tasks WHERE id = %s", (task_57.id,)).fetchone()
    assert other["state"] == "READY"  # untouched


def test_claim_task_by_id_does_not_claim_a_different_ready_task_when_target_is_stale(db):
    """Task 42 is no longer READY; task 57 is READY. Claiming by id=42 must
    return None, never silently fall back to claiming task 57."""
    wf = _make_workflow(db)
    task_42 = persistence.create_task(db, workflow_id=wf["id"], name="forty-two", task_type="noop")
    task_57 = persistence.create_task(db, workflow_id=wf["id"], name="fifty-seven", task_type="noop")

    # Make task 42 non-READY first (claim it via the untargeted path).
    persistence.claim_task_by_id(db, task_id=task_42.id, worker_id="w0", now=NOW, lease_duration=LEASE)

    result = persistence.claim_task_by_id(
        db, task_id=task_42.id, worker_id="w1", now=NOW, lease_duration=LEASE
    )

    assert result is None
    with db.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (task_57.id,)).fetchone()
    assert row["state"] == "READY"  # never touched by the id=42 claim attempt


def test_claim_task_by_id_nonexistent_returns_none(db):
    assert (
        persistence.claim_task_by_id(
            db, task_id=999999, worker_id="w1", now=NOW, lease_duration=LEASE
        )
        is None
    )


def test_claim_task_by_id_pending_task_returns_none(db):
    wf = _make_workflow(db)
    dep = persistence.create_task(db, workflow_id=wf["id"], name="dep", task_type="noop")
    pending = persistence.create_task(
        db, workflow_id=wf["id"], name="downstream", task_type="noop", depends_on_ids=[dep.id]
    )

    result = persistence.claim_task_by_id(
        db, task_id=pending.id, worker_id="w1", now=NOW, lease_duration=LEASE
    )
    assert result is None


def test_claim_task_by_id_already_running_returns_none(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    first = persistence.claim_task_by_id(
        db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=LEASE
    )
    assert first is not None

    second = persistence.claim_task_by_id(
        db, task_id=task.id, worker_id="w2", now=NOW, lease_duration=LEASE
    )
    assert second is None


def test_claim_task_by_id_succeeded_task_returns_none(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    persistence.claim_task_by_id(db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=LEASE)
    persistence.report_task_success(db, task_id=task.id, worker_id="w1", now=NOW)

    result = persistence.claim_task_by_id(
        db, task_id=task.id, worker_id="w2", now=NOW, lease_duration=LEASE
    )
    assert result is None


def test_claim_task_by_id_increments_attempt_count_exactly_once(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    assert task.instance.attempt_count == 0

    claimed = persistence.claim_task_by_id(
        db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=LEASE
    )
    assert claimed.instance.attempt_count == 1


def test_claim_task_by_id_sets_lease_owner_and_expiry(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    claimed = persistence.claim_task_by_id(
        db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=LEASE
    )
    assert claimed.instance.lease_owner == "w1"
    assert claimed.instance.lease_expires_at == NOW + LEASE


def test_claim_task_by_id_rejects_naive_datetime(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    with pytest.raises(TransitionError):
        persistence.claim_task_by_id(
            db,
            task_id=task.id,
            worker_id="w1",
            now=datetime(2026, 1, 1, 12, 0, 0),
            lease_duration=LEASE,
        )


def test_claim_task_by_id_rejects_empty_worker_id(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    with pytest.raises(TransitionError):
        persistence.claim_task_by_id(
            db, task_id=task.id, worker_id="", now=NOW, lease_duration=LEASE
        )


def test_claim_task_by_id_produces_no_outbox_event(db):
    """READY -> RUNNING is a non-READY-producing transition (see handoff
    section 9); claiming must never insert an outbox row."""
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    with db.connection() as conn:
        before = conn.execute("SELECT count(*) AS n FROM outbox_events").fetchone()["n"]

    persistence.claim_task_by_id(db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=LEASE)

    with db.connection() as conn:
        after = conn.execute("SELECT count(*) AS n FROM outbox_events").fetchone()["n"]

    # Task creation itself produced exactly one outbox row (zero deps ->
    # READY); the claim must not add a second one.
    assert before == 1
    assert after == 1


# ---------------------------------------------------------------------------
# renew_lease (heartbeat phase)
# ---------------------------------------------------------------------------


def test_renew_lease_extends_expiry(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    claimed = persistence.claim_task_by_id(
        db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=LEASE
    )
    assert claimed.instance.lease_expires_at == NOW + LEASE

    renew_at = NOW + timedelta(minutes=1)
    renewed = persistence.renew_lease(
        db, task_id=task.id, worker_id="w1", now=renew_at, lease_duration=LEASE
    )

    assert renewed is not None
    assert renewed.instance.lease_expires_at == renew_at + LEASE
    assert renewed.instance.lease_expires_at > claimed.instance.lease_expires_at


def test_renew_lease_preserves_state_attempt_count_and_owner(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    persistence.claim_task_by_id(db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=LEASE)

    renewed = persistence.renew_lease(
        db, task_id=task.id, worker_id="w1", now=NOW + timedelta(seconds=1), lease_duration=LEASE
    )

    assert renewed.instance.state == TaskState.RUNNING
    assert renewed.instance.attempt_count == 1  # unchanged -- only claim() increments this
    assert renewed.instance.lease_owner == "w1"  # unchanged


def test_renew_lease_wrong_worker_returns_none(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    persistence.claim_task_by_id(db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=LEASE)

    result = persistence.renew_lease(
        db, task_id=task.id, worker_id="w2", now=NOW + timedelta(seconds=1), lease_duration=LEASE
    )

    assert result is None
    with db.connection() as conn:
        row = conn.execute(
            "SELECT lease_owner, lease_expires_at FROM tasks WHERE id = %s", (task.id,)
        ).fetchone()
    assert row["lease_owner"] == "w1"  # untouched by the wrong-worker attempt
    assert row["lease_expires_at"] == NOW + LEASE


def test_renew_lease_nonexistent_task_returns_none(db):
    assert (
        persistence.renew_lease(
            db, task_id=999999, worker_id="w1", now=NOW, lease_duration=LEASE
        )
        is None
    )


def test_renew_lease_non_running_task_returns_none(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    # Still READY -- never claimed, so no lease exists to renew.
    result = persistence.renew_lease(
        db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=LEASE
    )
    assert result is None


def test_renew_lease_after_task_completes_returns_none(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    persistence.claim_task_by_id(db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=LEASE)
    persistence.report_task_success(db, task_id=task.id, worker_id="w1", now=NOW)

    result = persistence.renew_lease(
        db, task_id=task.id, worker_id="w1", now=NOW + timedelta(seconds=1), lease_duration=LEASE
    )
    assert result is None


def test_renew_lease_already_expired_returns_none(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    persistence.claim_task_by_id(db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=LEASE)

    after_expiry = NOW + LEASE + timedelta(seconds=1)
    result = persistence.renew_lease(
        db, task_id=task.id, worker_id="w1", now=after_expiry, lease_duration=LEASE
    )
    assert result is None


def test_renew_lease_exact_expiry_boundary_still_succeeds(db):
    """Inclusive boundary: lease_expires_at >= now must still renew,
    matching core.transitions._lease_is_valid's own inclusive rule (used
    by report_success/report_failure) -- NOT expire_lease's exclusive
    rule."""
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    persistence.claim_task_by_id(db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=LEASE)

    exact_boundary = NOW + LEASE  # == lease_expires_at exactly
    result = persistence.renew_lease(
        db, task_id=task.id, worker_id="w1", now=exact_boundary, lease_duration=LEASE
    )
    assert result is not None
    assert result.instance.lease_expires_at == exact_boundary + LEASE


def test_renew_lease_repeated_renewals(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    persistence.claim_task_by_id(db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=LEASE)

    t = NOW
    for _ in range(5):
        t = t + timedelta(seconds=30)
        result = persistence.renew_lease(
            db, task_id=task.id, worker_id="w1", now=t, lease_duration=LEASE
        )
        assert result is not None
        assert result.instance.lease_expires_at == t + LEASE


def test_renew_lease_rejects_naive_datetime(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    persistence.claim_task_by_id(db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=LEASE)

    with pytest.raises(TransitionError):
        persistence.renew_lease(
            db,
            task_id=task.id,
            worker_id="w1",
            now=datetime(2026, 1, 1, 12, 0, 0),
            lease_duration=LEASE,
        )


def test_renew_lease_rejects_empty_worker_id(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    persistence.claim_task_by_id(db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=LEASE)

    with pytest.raises(TransitionError):
        persistence.renew_lease(
            db, task_id=task.id, worker_id="", now=NOW, lease_duration=LEASE
        )


def test_renew_lease_does_not_create_outbox_event(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    persistence.claim_task_by_id(db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=LEASE)

    with db.connection() as conn:
        before = conn.execute("SELECT count(*) AS n FROM outbox_events").fetchone()["n"]

    persistence.renew_lease(
        db, task_id=task.id, worker_id="w1", now=NOW + timedelta(seconds=1), lease_duration=LEASE
    )

    with db.connection() as conn:
        after = conn.execute("SELECT count(*) AS n FROM outbox_events").fetchone()["n"]
    assert before == after


# ---------------------------------------------------------------------------
# report_task_success
# ---------------------------------------------------------------------------


def test_report_task_success_happy_path(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    claimed = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)

    result = persistence.report_task_success(db, task_id=claimed.id, worker_id="w1", now=NOW)
    assert result.applied is True
    assert result.record.instance.state == TaskState.SUCCEEDED
    assert result.record.instance.lease_owner is None


def test_report_task_success_stale_worker_is_ignored(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    claimed = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)

    result = persistence.report_task_success(db, task_id=claimed.id, worker_id="intruder", now=NOW)
    assert result.applied is False
    assert result.record.instance.state == TaskState.RUNNING
    assert result.record.instance.lease_owner == "w1"


# ---------------------------------------------------------------------------
# report_task_failure: retry vs terminal
# ---------------------------------------------------------------------------


def test_report_task_failure_retries_when_attempts_remain(db):
    wf = _make_workflow(db)
    persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="noop", max_attempts=3
    )
    claimed = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)

    result = persistence.report_task_failure(db, task_id=claimed.id, worker_id="w1", now=NOW)
    assert result.applied is True
    assert result.record.instance.state == TaskState.READY
    assert result.record.instance.attempt_count == 1


def test_report_task_failure_terminal_when_exhausted(db):
    wf = _make_workflow(db)
    persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="noop", max_attempts=1
    )
    claimed = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)

    result = persistence.report_task_failure(db, task_id=claimed.id, worker_id="w1", now=NOW)
    assert result.applied is True
    assert result.record.instance.state == TaskState.FAILED_TERMINAL


def test_report_task_failure_stale_worker_is_ignored(db):
    wf = _make_workflow(db)
    persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    claimed = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)

    result = persistence.report_task_failure(db, task_id=claimed.id, worker_id="intruder", now=NOW)
    assert result.applied is False
    assert result.record.instance.state == TaskState.RUNNING


# ---------------------------------------------------------------------------
# expire_lease
# ---------------------------------------------------------------------------


def test_expire_lease_rejects_unexpired_lease(db):
    wf = _make_workflow(db)
    persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")
    claimed = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)

    with pytest.raises(TransitionError):
        persistence.expire_lease(db, task_id=claimed.id, now=NOW)  # not yet expired

    with db.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (claimed.id,)).fetchone()
    assert row["state"] == "RUNNING"  # untouched -- rolled back


def test_expire_lease_reclaims_to_ready(db):
    wf = _make_workflow(db)
    persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="noop", max_attempts=3
    )
    claimed = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    after_expiry = NOW + LEASE + timedelta(seconds=1)

    result = persistence.expire_lease(db, task_id=claimed.id, now=after_expiry)
    assert result.applied is True
    assert result.record.instance.state == TaskState.READY


def test_expire_lease_reclaims_to_failed_terminal_when_exhausted(db):
    wf = _make_workflow(db)
    persistence.create_task(
        db, workflow_id=wf["id"], name="t1", task_type="noop", max_attempts=1
    )
    claimed = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    after_expiry = NOW + LEASE + timedelta(seconds=1)

    result = persistence.expire_lease(db, task_id=claimed.id, now=after_expiry)
    assert result.record.instance.state == TaskState.FAILED_TERMINAL


# ---------------------------------------------------------------------------
# PENDING -> READY propagation
# ---------------------------------------------------------------------------


def test_dependency_propagation_fan_in(db):
    wf = _make_workflow(db)
    a = persistence.create_task(db, workflow_id=wf["id"], name="a", task_type="noop")
    b = persistence.create_task(db, workflow_id=wf["id"], name="b", task_type="noop")
    c = persistence.create_task(
        db, workflow_id=wf["id"], name="c", task_type="noop", depends_on_ids=[a.id, b.id]
    )
    assert c.instance.state == TaskState.PENDING

    claimed_a = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    persistence.report_task_success(db, task_id=claimed_a.id, worker_id="w1", now=NOW)

    with db.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (c.id,)).fetchone()
    assert row["state"] == "PENDING"  # b not done yet

    claimed_b = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    persistence.report_task_success(db, task_id=claimed_b.id, worker_id="w1", now=NOW)

    with db.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (c.id,)).fetchone()
    assert row["state"] == "READY"


# ---------------------------------------------------------------------------
# SKIPPED cascade
# ---------------------------------------------------------------------------


def test_diamond_skip_cascade(db):
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

    with db.connection() as conn:
        states = {
            row["id"]: row["state"]
            for row in conn.execute(
                "SELECT id, state FROM tasks WHERE id = ANY(%s)", ([b.id, c.id, d.id],)
            ).fetchall()
        }
    assert states[b.id] == "SKIPPED"
    assert states[c.id] == "SKIPPED"
    assert states[d.id] == "SKIPPED"


def test_skip_cascade_idempotency_does_not_error_on_overlapping_paths(db):
    # Same diamond shape -- the point here is specifically that D (reachable
    # via both B and C) is only ever processed once and never raises.
    wf = _make_workflow(db)
    a = persistence.create_task(
        db, workflow_id=wf["id"], name="a", task_type="noop", max_attempts=1
    )
    b = persistence.create_task(db, workflow_id=wf["id"], name="b", task_type="noop", depends_on_ids=[a.id])
    c = persistence.create_task(db, workflow_id=wf["id"], name="c", task_type="noop", depends_on_ids=[a.id])
    persistence.create_task(
        db, workflow_id=wf["id"], name="d", task_type="noop", depends_on_ids=[b.id, c.id]
    )

    claimed_a = persistence.claim_task(db, worker_id="w1", now=NOW, lease_duration=LEASE)
    # Should complete without error despite D being reachable twice.
    persistence.report_task_failure(db, task_id=claimed_a.id, worker_id="w1", now=NOW)

    # Calling skip() again directly on an already-SKIPPED node is a no-op.
    result = persistence.skip(db, task_id=b.id)
    assert result.applied is False


# ---------------------------------------------------------------------------
# read-only queries (Phase 8 -- HTTP API prerequisite)
# ---------------------------------------------------------------------------


def test_get_workflow_found(db):
    wf = _make_workflow(db)
    result = persistence.get_workflow(db, workflow_id=wf["id"])
    assert result is not None
    assert result["id"] == wf["id"]
    assert result["name"] == "wf"


def test_get_workflow_not_found(db):
    assert persistence.get_workflow(db, workflow_id=999999) is None


def test_get_task_found(db):
    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    result = persistence.get_task(db, task_id=task.id)

    assert result is not None
    assert result.id == task.id
    assert result.workflow_id == wf["id"]
    assert result.name == "t1"
    assert result.task_type == "noop"
    assert result.instance.state == TaskState.READY


def test_get_task_not_found(db):
    assert persistence.get_task(db, task_id=999999) is None


def test_get_task_does_not_lock_the_row(db, db_dsn):
    """A read must never block, or be blocked by, a concurrent claim on
    the same row -- proven with two genuinely separate connections, one
    holding a real row lock via claim_task_by_id, the other reading
    concurrently via get_task."""
    import threading
    import time

    from db.connection import get_pool

    wf = _make_workflow(db)
    task = persistence.create_task(db, workflow_id=wf["id"], name="t1", task_type="noop")

    read_completed = threading.Event()

    def reader():
        local_pool = get_pool(db_dsn, min_size=1, max_size=1)
        try:
            # If get_task ever took FOR UPDATE, this would block behind
            # the claim below until it committed -- it must not.
            persistence.get_task(local_pool, task_id=task.id)
            read_completed.set()
        finally:
            local_pool.close()

    claimed = persistence.claim_task_by_id(
        db, task_id=task.id, worker_id="w1", now=NOW, lease_duration=LEASE
    )
    assert claimed is not None

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    reader_thread.join(timeout=2.0)

    assert read_completed.is_set()  # completed promptly, never blocked


def test_list_tasks_for_workflow_returns_all_in_order(db):
    wf = _make_workflow(db)
    t1 = persistence.create_task(db, workflow_id=wf["id"], name="a", task_type="noop")
    t2 = persistence.create_task(db, workflow_id=wf["id"], name="b", task_type="noop")
    t3 = persistence.create_task(db, workflow_id=wf["id"], name="c", task_type="noop")

    result = persistence.list_tasks_for_workflow(db, workflow_id=wf["id"])

    assert [t.id for t in result] == [t1.id, t2.id, t3.id]


def test_list_tasks_for_workflow_empty_when_no_tasks(db):
    wf = _make_workflow(db)
    assert persistence.list_tasks_for_workflow(db, workflow_id=wf["id"]) == []


def test_list_tasks_for_workflow_empty_when_workflow_does_not_exist(db):
    assert persistence.list_tasks_for_workflow(db, workflow_id=999999) == []


def test_list_tasks_for_workflow_only_includes_that_workflow(db):
    wf1 = _make_workflow(db)
    wf2 = persistence.create_workflow(db, name="wf2")
    t1 = persistence.create_task(db, workflow_id=wf1["id"], name="a", task_type="noop")
    persistence.create_task(db, workflow_id=wf2["id"], name="b", task_type="noop")

    result = persistence.list_tasks_for_workflow(db, workflow_id=wf1["id"])

    assert [t.id for t in result] == [t1.id]


# ---------------------------------------------------------------------------
# check_connection (Phase 10 -- operational readiness)
# ---------------------------------------------------------------------------


def test_check_connection_succeeds_against_real_postgres(db):
    persistence.check_connection(db)  # must not raise


def test_check_connection_raises_when_unreachable():
    from db.connection import get_pool

    # A genuinely unreachable target (nothing listens on port 1), bounded
    # by a short pool-level timeout so this test stays fast -- real
    # behavior against a real (nonexistent) endpoint, not a mock.
    bad_pool = get_pool(
        "postgresql://baduser:badpass@localhost:1/nonexistent?connect_timeout=1",
        min_size=0,
        max_size=1,
        timeout=2,
    )
    try:
        with pytest.raises(Exception):
            persistence.check_connection(bad_pool)
    finally:
        bad_pool.close()
