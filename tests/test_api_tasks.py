"""Phase 8: API tests for the task endpoints. Real PostgreSQL throughout.

Also exercises persistence functions directly (``from db import
persistence``) alongside the API, specifically to prove the API is a
thin front door onto the SAME underlying state -- not a separate
pathway -- per the Phase 8 architecture constraint."""

from __future__ import annotations

from db import persistence


def _create_workflow(api_client, name="wf"):
    return api_client.post("/workflows", json={"name": name}).json()


# ---------------------------------------------------------------------------
# creation: zero deps -> READY, with deps -> PENDING
# ---------------------------------------------------------------------------


def test_create_task_without_dependencies_is_ready(api_client):
    wf = _create_workflow(api_client)

    response = api_client.post(
        f"/workflows/{wf['id']}/tasks",
        json={"name": "t1", "task_type": "noop"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["state"] == "READY"
    assert body["workflow_id"] == wf["id"]
    assert body["attempt_count"] == 0
    assert body["max_attempts"] == 1
    assert body["lease_owner"] is None


def test_create_task_with_dependencies_is_pending(api_client):
    wf = _create_workflow(api_client)
    dep = api_client.post(
        f"/workflows/{wf['id']}/tasks", json={"name": "dep", "task_type": "noop"}
    ).json()

    response = api_client.post(
        f"/workflows/{wf['id']}/tasks",
        json={"name": "downstream", "task_type": "noop", "depends_on_ids": [dep["id"]]},
    )

    assert response.status_code == 201
    assert response.json()["state"] == "PENDING"


def test_create_task_with_payload_and_max_attempts(api_client):
    wf = _create_workflow(api_client)

    response = api_client.post(
        f"/workflows/{wf['id']}/tasks",
        json={
            "name": "t1",
            "task_type": "noop",
            "max_attempts": 5,
            "payload": {"key": "value", "n": 3},
        },
    )

    body = response.json()
    assert body["max_attempts"] == 5
    assert body["payload"] == {"key": "value", "n": 3}


# ---------------------------------------------------------------------------
# outbox invariant preserved through the API
# ---------------------------------------------------------------------------


def test_create_task_preserves_outbox_invariant(api_client, db):
    """A task created via the API with zero deps must still produce
    exactly one outbox row -- proving the API doesn't bypass the Phase 2
    transactional-outbox guarantee."""
    wf = _create_workflow(api_client)

    response = api_client.post(
        f"/workflows/{wf['id']}/tasks", json={"name": "t1", "task_type": "noop"}
    )
    task_id = response.json()["id"]

    with db.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM outbox_events WHERE task_id = %s", (task_id,)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["payload"]["task_id"] == task_id
    assert rows[0]["payload"]["task_type"] == "noop"


def test_create_task_with_dependencies_produces_no_outbox_row(api_client, db):
    wf = _create_workflow(api_client)
    dep = api_client.post(
        f"/workflows/{wf['id']}/tasks", json={"name": "dep", "task_type": "noop"}
    ).json()

    response = api_client.post(
        f"/workflows/{wf['id']}/tasks",
        json={"name": "downstream", "task_type": "noop", "depends_on_ids": [dep["id"]]},
    )
    task_id = response.json()["id"]

    with db.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM outbox_events WHERE task_id = %s", (task_id,)
        ).fetchall()
    assert rows == []


# ---------------------------------------------------------------------------
# 404s
# ---------------------------------------------------------------------------


def test_create_task_for_nonexistent_workflow_returns_404(api_client):
    response = api_client.post(
        "/workflows/999999/tasks", json={"name": "t1", "task_type": "noop"}
    )
    assert response.status_code == 404


def test_get_task_not_found_returns_404(api_client):
    response = api_client.get("/tasks/999999")
    assert response.status_code == 404


def test_get_task_invalid_id_returns_422(api_client):
    response = api_client.get("/tasks/not-an-integer")
    assert response.status_code == 422


def test_list_tasks_for_nonexistent_workflow_returns_404(api_client):
    response = api_client.get("/workflows/999999/tasks")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# invalid dependency -> existing rollback behavior remains intact
# ---------------------------------------------------------------------------


def test_create_task_with_invalid_dependency_returns_400_and_rolls_back(api_client, db):
    wf = _create_workflow(api_client)

    response = api_client.post(
        f"/workflows/{wf['id']}/tasks",
        json={"name": "bad", "task_type": "noop", "depends_on_ids": [999999]},
    )

    assert response.status_code == 400

    # The existing rollback guarantee (db/persistence.py::create_task) is
    # untouched: no orphaned task row was left behind.
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE name = %s", ("bad",)
        ).fetchall()
    assert rows == []


def test_task_creation_cannot_form_a_dependency_cycle(api_client):
    """A workflow's DAG is acyclic by construction, not by any explicit
    cycle-detection check: depends_on_ids may only name tasks that
    already exist (enforced by the tasks.id foreign key on
    task_dependencies -- see db/migrations/0001_init.sql), and there is
    no endpoint to add a dependency edge to an already-created task after
    the fact. A task can therefore never depend on a task created after
    it, which is exactly what a cycle would require. This test pins that
    structural guarantee rather than any new validation logic."""
    wf = _create_workflow(api_client)
    a = api_client.post(
        f"/workflows/{wf['id']}/tasks", json={"name": "a", "task_type": "noop"}
    ).json()

    # b depends on a (fine) -- a "future" id one greater than any task
    # created so far stands in for "the task that would close the cycle
    # back to a hasn't been created yet, and never can be after the fact".
    future_id = a["id"] + 1000
    response = api_client.post(
        f"/workflows/{wf['id']}/tasks",
        json={"name": "b", "task_type": "noop", "depends_on_ids": [a["id"], future_id]},
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# GET reflects live state, not a cached/stale view
# ---------------------------------------------------------------------------


def test_get_task_reflects_state_change_made_directly_through_persistence(api_client, db):
    from datetime import datetime, timedelta, timezone

    wf = _create_workflow(api_client)
    created = api_client.post(
        f"/workflows/{wf['id']}/tasks", json={"name": "t1", "task_type": "noop"}
    ).json()
    assert created["state"] == "READY"

    # Mutate state directly through the persistence layer -- NOT through
    # the API (there is no state-mutation endpoint, by design).
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    persistence.claim_task_by_id(
        db, task_id=created["id"], worker_id="w1", now=now, lease_duration=timedelta(minutes=5)
    )

    response = api_client.get(f"/tasks/{created['id']}")

    body = response.json()
    assert body["state"] == "RUNNING"
    assert body["lease_owner"] == "w1"
    assert body["attempt_count"] == 1


# ---------------------------------------------------------------------------
# workflow task listing
# ---------------------------------------------------------------------------


def test_list_workflow_tasks_returns_all_tasks(api_client):
    wf = _create_workflow(api_client)
    t1 = api_client.post(
        f"/workflows/{wf['id']}/tasks", json={"name": "a", "task_type": "noop"}
    ).json()
    t2 = api_client.post(
        f"/workflows/{wf['id']}/tasks", json={"name": "b", "task_type": "noop"}
    ).json()

    response = api_client.get(f"/workflows/{wf['id']}/tasks")

    assert response.status_code == 200
    ids = [t["id"] for t in response.json()]
    assert ids == [t1["id"], t2["id"]]


def test_list_workflow_tasks_empty_when_no_tasks(api_client):
    wf = _create_workflow(api_client)
    response = api_client.get(f"/workflows/{wf['id']}/tasks")
    assert response.status_code == 200
    assert response.json() == []


def test_list_workflow_tasks_only_includes_own_workflow(api_client):
    wf1 = _create_workflow(api_client, name="wf1")
    wf2 = _create_workflow(api_client, name="wf2")
    t1 = api_client.post(
        f"/workflows/{wf1['id']}/tasks", json={"name": "a", "task_type": "noop"}
    ).json()
    api_client.post(f"/workflows/{wf2['id']}/tasks", json={"name": "b", "task_type": "noop"})

    response = api_client.get(f"/workflows/{wf1['id']}/tasks")

    ids = [t["id"] for t in response.json()]
    assert ids == [t1["id"]]


# ---------------------------------------------------------------------------
# request validation
# ---------------------------------------------------------------------------


def test_create_task_missing_required_field_returns_422(api_client):
    wf = _create_workflow(api_client)
    response = api_client.post(f"/workflows/{wf['id']}/tasks", json={"name": "t1"})
    assert response.status_code == 422


def test_create_task_wrong_type_for_depends_on_ids_returns_422(api_client):
    wf = _create_workflow(api_client)
    response = api_client.post(
        f"/workflows/{wf['id']}/tasks",
        json={"name": "t1", "task_type": "noop", "depends_on_ids": "not-a-list"},
    )
    assert response.status_code == 422


def test_create_task_malformed_json_returns_422(api_client):
    wf = _create_workflow(api_client)
    response = api_client.post(
        f"/workflows/{wf['id']}/tasks",
        content=b"{not valid",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
