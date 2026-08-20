"""Phase 12: API tests for GET /workflows/{id}/status -- the caller-facing
workflow completion/rollup view. Real PostgreSQL throughout (via the
``api_client`` fixture's own real connection pool -- see
tests/conftest.py), same convention as every other API test file.

Non-READY/PENDING states are produced by calling the existing,
unmodified persistence functions directly against the ``db`` fixture --
exactly the pattern tests/test_api_tasks.py already established
(``test_get_task_reflects_state_change_made_directly_through_persistence``)
-- there is no state-mutation endpoint, by design, and this phase adds
none."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from db import persistence

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)


def _create_workflow(api_client, name="wf"):
    return api_client.post("/workflows", json={"name": name}).json()


def _create_task(api_client, wf_id, *, name="t", max_attempts=1, depends_on_ids=None):
    body = {"name": name, "task_type": "noop", "max_attempts": max_attempts}
    if depends_on_ids is not None:
        body["depends_on_ids"] = depends_on_ids
    return api_client.post(f"/workflows/{wf_id}/tasks", json=body).json()


def _claim(db, task_id, *, worker_id="w1"):
    return persistence.claim_task_by_id(
        db, task_id=task_id, worker_id=worker_id, now=NOW, lease_duration=LEASE
    )


def _make_succeeded(api_client, db, wf_id, *, name="succeeded"):
    task = _create_task(api_client, wf_id, name=name)
    _claim(db, task["id"])
    persistence.report_task_success(db, task_id=task["id"], worker_id="w1", now=NOW)
    return task


def _make_failed_terminal(api_client, db, wf_id, *, name="failed"):
    task = _create_task(api_client, wf_id, name=name, max_attempts=1)
    _claim(db, task["id"])
    persistence.report_task_failure(db, task_id=task["id"], worker_id="w1", now=NOW)
    return task


def _status(api_client, wf_id):
    response = api_client.get(f"/workflows/{wf_id}/status")
    assert response.status_code == 200
    return response.json()


# ---------------------------------------------------------------------------
# 404
# ---------------------------------------------------------------------------


def test_status_nonexistent_workflow_returns_404(api_client):
    response = api_client.get("/workflows/999999/status")
    assert response.status_code == 404


def test_status_invalid_id_returns_422(api_client):
    response = api_client.get("/workflows/not-an-integer/status")
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# empty workflow
# ---------------------------------------------------------------------------


def test_status_empty_workflow_is_incomplete(api_client):
    wf = _create_workflow(api_client)

    body = _status(api_client, wf["id"])

    assert body["workflow_id"] == wf["id"]
    assert body["total_tasks"] == 0
    for key in ("pending", "ready", "running", "succeeded", "failed_terminal", "skipped"):
        assert body[key] == 0
    # An empty workflow is deliberately NOT complete -- see
    # api/schemas.py::WorkflowStatusResponse's docstring for why this is
    # the smallest defensible choice.
    assert body["is_complete"] is False
    assert body["has_failures"] is False


# ---------------------------------------------------------------------------
# all-SUCCEEDED -> complete, no failures
# ---------------------------------------------------------------------------


def test_status_all_succeeded_is_complete_with_no_failures(api_client, db):
    wf = _create_workflow(api_client)
    _make_succeeded(api_client, db, wf["id"], name="a")
    _make_succeeded(api_client, db, wf["id"], name="b")

    body = _status(api_client, wf["id"])

    assert body["total_tasks"] == 2
    assert body["succeeded"] == 2
    assert body["pending"] == body["ready"] == body["running"] == 0
    assert body["failed_terminal"] == body["skipped"] == 0
    assert body["is_complete"] is True
    assert body["has_failures"] is False


# ---------------------------------------------------------------------------
# active states (PENDING/READY/RUNNING) -> incomplete
# ---------------------------------------------------------------------------


def test_status_ready_task_is_incomplete(api_client):
    wf = _create_workflow(api_client)
    _create_task(api_client, wf["id"], name="r")

    body = _status(api_client, wf["id"])

    assert body["ready"] == 1
    assert body["is_complete"] is False
    assert body["has_failures"] is False


def test_status_pending_task_is_incomplete(api_client):
    wf = _create_workflow(api_client)
    dep = _create_task(api_client, wf["id"], name="dep")
    _create_task(api_client, wf["id"], name="downstream", depends_on_ids=[dep["id"]])

    body = _status(api_client, wf["id"])

    assert body["pending"] == 1
    assert body["ready"] == 1  # the unclaimed dependency itself
    assert body["is_complete"] is False


def test_status_running_task_is_incomplete(api_client, db):
    wf = _create_workflow(api_client)
    task = _create_task(api_client, wf["id"], name="r")
    _claim(db, task["id"])

    body = _status(api_client, wf["id"])

    assert body["running"] == 1
    assert body["is_complete"] is False
    assert body["has_failures"] is False


# ---------------------------------------------------------------------------
# FAILED_TERMINAL -> has_failures
# ---------------------------------------------------------------------------


def test_status_failed_terminal_has_failures(api_client, db):
    wf = _create_workflow(api_client)
    _make_failed_terminal(api_client, db, wf["id"])

    body = _status(api_client, wf["id"])

    assert body["failed_terminal"] == 1
    assert body["is_complete"] is True
    assert body["has_failures"] is True


# ---------------------------------------------------------------------------
# SKIPPED-only -> complete, but NOT a failure
# ---------------------------------------------------------------------------


def test_status_skipped_only_is_complete_with_no_failures(api_client, db):
    """A parent that fails terminally cascades SKIPPED to its PENDING
    dependent (existing, unmodified db/persistence.py skip-cascade
    behavior) -- the parent itself is FAILED_TERMINAL, so this is not
    actually a SKIPPED-only workflow at the row level, but proves the
    distinct case the brief asks for: has_failures must reflect the real
    FAILED_TERMINAL count, and SKIPPED must never be conflated with it."""
    wf = _create_workflow(api_client)
    parent = _create_task(api_client, wf["id"], name="parent", max_attempts=1)
    _create_task(api_client, wf["id"], name="child", depends_on_ids=[parent["id"]])
    _claim(db, parent["id"])
    persistence.report_task_failure(db, task_id=parent["id"], worker_id="w1", now=NOW)

    body = _status(api_client, wf["id"])

    assert body["failed_terminal"] == 1
    assert body["skipped"] == 1
    assert body["is_complete"] is True
    assert body["has_failures"] is True


def test_status_purely_skipped_task_does_not_count_as_a_failure(api_client, db):
    """Isolates the SKIPPED count itself: once a task is SKIPPED, it
    contributes to ``skipped``, never to ``failed_terminal`` or
    ``has_failures`` -- checked directly against the persistence layer's
    ``skip()`` (PENDING -> SKIPPED with no failure involved at all, e.g.
    a task explicitly skipped rather than cascaded)."""
    wf = _create_workflow(api_client)
    dep = _create_task(api_client, wf["id"], name="dep")
    pending = _create_task(api_client, wf["id"], name="pending", depends_on_ids=[dep["id"]])
    persistence.skip(db, task_id=pending["id"])

    body = _status(api_client, wf["id"])

    assert body["skipped"] == 1
    assert body["failed_terminal"] == 0
    assert body["has_failures"] is False
    # dep itself is still READY -> workflow remains incomplete regardless.
    assert body["is_complete"] is False


# ---------------------------------------------------------------------------
# mixed terminal states
# ---------------------------------------------------------------------------


def test_status_mixed_succeeded_and_failed_terminal_is_complete_and_has_failures(
    api_client, db
):
    wf = _create_workflow(api_client)
    _make_succeeded(api_client, db, wf["id"], name="ok")
    _make_failed_terminal(api_client, db, wf["id"], name="bad")

    body = _status(api_client, wf["id"])

    assert body["succeeded"] == 1
    assert body["failed_terminal"] == 1
    assert body["is_complete"] is True
    assert body["has_failures"] is True


def test_status_mixed_active_and_terminal_is_incomplete(api_client, db):
    wf = _create_workflow(api_client)
    _make_succeeded(api_client, db, wf["id"], name="ok")
    _create_task(api_client, wf["id"], name="still-ready")

    body = _status(api_client, wf["id"])

    assert body["succeeded"] == 1
    assert body["ready"] == 1
    assert body["is_complete"] is False


# ---------------------------------------------------------------------------
# every state present at once -- counts, not just booleans
# ---------------------------------------------------------------------------


def test_status_counts_are_correct_for_every_state_simultaneously(api_client, db):
    wf = _create_workflow(api_client)

    # PENDING, blocked on a RUNNING dependency.
    blocker = _create_task(api_client, wf["id"], name="blocker")
    pending = _create_task(
        api_client, wf["id"], name="pending", depends_on_ids=[blocker["id"]]
    )
    _claim(db, blocker["id"])  # blocker -> RUNNING; pending stays PENDING

    _create_task(api_client, wf["id"], name="ready")  # READY, untouched
    _make_succeeded(api_client, db, wf["id"], name="succeeded")

    parent = _create_task(api_client, wf["id"], name="parent", max_attempts=1)
    _create_task(api_client, wf["id"], name="child", depends_on_ids=[parent["id"]])
    _claim(db, parent["id"])
    persistence.report_task_failure(db, task_id=parent["id"], worker_id="w1", now=NOW)

    body = _status(api_client, wf["id"])

    assert body["total_tasks"] == 6
    assert body["pending"] == 1
    assert body["ready"] == 1
    assert body["running"] == 1
    assert body["succeeded"] == 1
    assert body["failed_terminal"] == 1
    assert body["skipped"] == 1
    assert body["is_complete"] is False  # PENDING/READY/RUNNING all present
    assert body["has_failures"] is True

    assert pending["state"] == "PENDING"  # sanity: the fixture behaved as intended


# ---------------------------------------------------------------------------
# reflects live DB state, not any cached/application-held view
# ---------------------------------------------------------------------------


def test_status_reflects_state_changed_directly_through_persistence(api_client, db):
    wf = _create_workflow(api_client)
    task = _create_task(api_client, wf["id"], name="t")

    before = _status(api_client, wf["id"])
    assert before["ready"] == 1
    assert before["running"] == 0
    assert before["is_complete"] is False

    # Mutate state directly through the persistence layer, exactly as
    # tests/test_api_tasks.py's equivalent test does -- not through any
    # API endpoint (there is no state-mutation endpoint, by design).
    _claim(db, task["id"])
    persistence.report_task_success(db, task_id=task["id"], worker_id="w1", now=NOW)

    after = _status(api_client, wf["id"])
    assert after["ready"] == 0
    assert after["running"] == 0
    assert after["succeeded"] == 1
    assert after["is_complete"] is True
