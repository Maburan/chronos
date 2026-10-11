"""Persistence-layer tests for the Definition vs Run model (Phase 2):

  - save_workflow_definition: whole-graph replace, cycle rejection,
    unknown/self-dependency rejection.
  - create_workflow_run: atomic materialization into real tasks/
    task_dependencies rows, outbox events, run isolation across repeated
    runs, and immutability of a run's recorded meaning against later
    definition edits.

Requires a real PostgreSQL database (see tests/conftest.py's ``db``
fixture) -- these are integration tests, not unit tests.
"""

from __future__ import annotations

import pytest

from core import transitions as core_transitions
from core.transitions import TaskState
from db import persistence


def _make_workflow(db):
    return persistence.create_workflow(db, name="wf", user_id=db.test_user_id)


# ---------------------------------------------------------------------------
# save_workflow_definition
# ---------------------------------------------------------------------------


def test_save_workflow_definition_creates_tasks_and_dependencies(db):
    wf = _make_workflow(db)

    rows = persistence.save_workflow_definition(
        db,
        workflow_id=wf["id"],
        tasks=[
            {"client_id": "a", "name": "fetch", "task_type": "http_request"},
            {"client_id": "b", "name": "notify", "task_type": "echo"},
        ],
        dependencies=[{"task_client_id": "b", "depends_on_client_id": "a"}],
    )

    by_name = {r["name"]: r for r in rows}
    assert by_name["fetch"]["depends_on_ids"] == ()
    assert by_name["notify"]["depends_on_ids"] == (by_name["fetch"]["id"],)

    # A definition save must never create an executable task row -- it is
    # purely design-time.
    assert persistence.list_tasks_for_workflow(db, workflow_id=wf["id"]) == []


def test_save_workflow_definition_replaces_whole_graph(db):
    wf = _make_workflow(db)

    persistence.save_workflow_definition(
        db,
        workflow_id=wf["id"],
        tasks=[{"client_id": "a", "name": "first", "task_type": "echo"}],
        dependencies=[],
    )
    rows = persistence.save_workflow_definition(
        db,
        workflow_id=wf["id"],
        tasks=[{"client_id": "x", "name": "second", "task_type": "echo"}],
        dependencies=[],
    )

    definitions = persistence.get_workflow_definition(db, workflow_id=wf["id"])
    assert [d["name"] for d in definitions] == ["second"]
    assert len(rows) == 1


def test_save_workflow_definition_rejects_cycle(db):
    wf = _make_workflow(db)

    with pytest.raises(persistence.CyclicDependencyError):
        persistence.save_workflow_definition(
            db,
            workflow_id=wf["id"],
            tasks=[
                {"client_id": "a", "name": "a", "task_type": "echo"},
                {"client_id": "b", "name": "b", "task_type": "echo"},
            ],
            dependencies=[
                {"task_client_id": "a", "depends_on_client_id": "b"},
                {"task_client_id": "b", "depends_on_client_id": "a"},
            ],
        )

    # Rejected before any row was written -- the transaction never opened.
    assert persistence.get_workflow_definition(db, workflow_id=wf["id"]) == []


def test_save_workflow_definition_rejects_unknown_dependency_client_id(db):
    wf = _make_workflow(db)
    with pytest.raises(KeyError):
        persistence.save_workflow_definition(
            db,
            workflow_id=wf["id"],
            tasks=[{"client_id": "a", "name": "a", "task_type": "echo"}],
            dependencies=[{"task_client_id": "a", "depends_on_client_id": "ghost"}],
        )


def test_save_workflow_definition_rejects_self_dependency(db):
    wf = _make_workflow(db)
    with pytest.raises(ValueError):
        persistence.save_workflow_definition(
            db,
            workflow_id=wf["id"],
            tasks=[{"client_id": "a", "name": "a", "task_type": "echo"}],
            dependencies=[{"task_client_id": "a", "depends_on_client_id": "a"}],
        )


def test_get_workflow_definition_empty_for_new_workflow(db):
    wf = _make_workflow(db)
    assert persistence.get_workflow_definition(db, workflow_id=wf["id"]) == []


# ---------------------------------------------------------------------------
# create_workflow_run -- materialization, atomicity, immutability, isolation
# ---------------------------------------------------------------------------


def test_create_workflow_run_raises_when_no_definition(db):
    wf = _make_workflow(db)
    with pytest.raises(persistence.EmptyWorkflowDefinitionError):
        persistence.create_workflow_run(
            db, workflow_id=wf["id"], triggered_by_user_id=db.test_user_id
        )


def test_create_workflow_run_materializes_tasks_matching_definition(db):
    wf = _make_workflow(db)
    persistence.save_workflow_definition(
        db,
        workflow_id=wf["id"],
        tasks=[
            {"client_id": "a", "name": "fetch", "task_type": "http_request", "payload": {"url": "https://example.com"}},
            {"client_id": "b", "name": "notify", "task_type": "echo"},
        ],
        dependencies=[{"task_client_id": "b", "depends_on_client_id": "a"}],
    )

    run = persistence.create_workflow_run(
        db, workflow_id=wf["id"], triggered_by_user_id=db.test_user_id
    )
    assert run["workflow_id"] == wf["id"]
    assert run["triggered_by_user_id"] == db.test_user_id

    tasks = persistence.list_tasks_for_run(db, run_id=run["id"])
    by_name = {t.name: t for t in tasks}
    assert set(by_name) == {"fetch", "notify"}
    assert by_name["fetch"].instance.state == TaskState.READY
    assert by_name["notify"].instance.state == TaskState.PENDING
    assert by_name["notify"].depends_on_ids == (by_name["fetch"].id,)
    assert by_name["fetch"].workflow_run_id == run["id"]
    assert by_name["fetch"].task_definition_id is not None
    assert by_name["fetch"].payload == {"url": "https://example.com"}


def test_create_workflow_run_zero_dependency_task_gets_outbox_event(db):
    wf = _make_workflow(db)
    persistence.save_workflow_definition(
        db,
        workflow_id=wf["id"],
        tasks=[{"client_id": "a", "name": "solo", "task_type": "echo"}],
        dependencies=[],
    )
    run = persistence.create_workflow_run(
        db, workflow_id=wf["id"], triggered_by_user_id=db.test_user_id
    )
    tasks = persistence.list_tasks_for_run(db, run_id=run["id"])
    assert len(tasks) == 1

    with db.connection() as conn:
        row = conn.execute(
            "SELECT * FROM outbox_events WHERE task_id = %s", (tasks[0].id,)
        ).fetchone()
    assert row is not None
    assert row["event_type"] == "TASK_READY"


def test_create_workflow_run_is_atomic_on_mid_materialization_failure(db, monkeypatch):
    """If materialization fails partway through, nothing for this attempt
    is left behind -- no workflow_runs row, no tasks rows, no outbox
    events. Simulated by making the second call to
    core.transitions.create_task raise, then asserting the whole attempt
    left zero trace (proves the transaction, not application code, is what
    guarantees atomicity)."""
    wf = _make_workflow(db)
    persistence.save_workflow_definition(
        db,
        workflow_id=wf["id"],
        tasks=[
            {"client_id": "a", "name": "first", "task_type": "echo"},
            {"client_id": "b", "name": "second", "task_type": "echo"},
        ],
        dependencies=[],
    )

    real_create_task = core_transitions.create_task
    call_count = {"n": 0}

    def _flaky_create_task(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated mid-materialization crash")
        return real_create_task(*args, **kwargs)

    monkeypatch.setattr(persistence.transitions, "create_task", _flaky_create_task)

    with pytest.raises(RuntimeError, match="simulated mid-materialization crash"):
        persistence.create_workflow_run(
            db, workflow_id=wf["id"], triggered_by_user_id=db.test_user_id
        )

    assert persistence.list_workflow_runs(db, workflow_id=wf["id"]) == []
    assert persistence.list_tasks_for_workflow(db, workflow_id=wf["id"]) == []
    with db.connection() as conn:
        count = conn.execute("SELECT count(*) AS n FROM outbox_events").fetchone()["n"]
    assert count == 0


def test_workflow_run_snapshot_is_immutable_to_later_definition_edits(db):
    wf = _make_workflow(db)
    persistence.save_workflow_definition(
        db,
        workflow_id=wf["id"],
        tasks=[{"client_id": "a", "name": "original-name", "task_type": "echo"}],
        dependencies=[],
    )
    run = persistence.create_workflow_run(
        db, workflow_id=wf["id"], triggered_by_user_id=db.test_user_id
    )

    # Edit the definition after the run has already been materialized.
    persistence.save_workflow_definition(
        db,
        workflow_id=wf["id"],
        tasks=[{"client_id": "x", "name": "renamed-and-different", "task_type": "sleep"}],
        dependencies=[],
    )

    reloaded_run = persistence.get_workflow_run(db, run_id=run["id"])
    snapshot_names = [t["name"] for t in reloaded_run["definition_snapshot"]["task_definitions"]]
    assert snapshot_names == ["original-name"]

    # The materialized tasks for that run are also unaffected -- they were
    # never linked to the live task_definitions row for correctness.
    tasks = persistence.list_tasks_for_run(db, run_id=run["id"])
    assert [t.name for t in tasks] == ["original-name"]
    assert [t.task_type for t in tasks] == ["echo"]


def test_workflow_run_snapshot_survives_definition_row_deletion(db):
    wf = _make_workflow(db)
    persistence.save_workflow_definition(
        db,
        workflow_id=wf["id"],
        tasks=[{"client_id": "a", "name": "solo", "task_type": "echo"}],
        dependencies=[],
    )
    run = persistence.create_workflow_run(
        db, workflow_id=wf["id"], triggered_by_user_id=db.test_user_id
    )

    # Deleting every task_definition (e.g. the workflow is redesigned from
    # scratch) must not erase what the run represents.
    persistence.save_workflow_definition(db, workflow_id=wf["id"], tasks=[], dependencies=[])

    reloaded_run = persistence.get_workflow_run(db, run_id=run["id"])
    assert reloaded_run["definition_snapshot"]["task_definitions"][0]["name"] == "solo"

    tasks = persistence.list_tasks_for_run(db, run_id=run["id"])
    assert len(tasks) == 1
    assert tasks[0].task_definition_id is None  # ON DELETE SET NULL fired
    assert tasks[0].name == "solo"  # but the task row itself is untouched


def test_running_a_workflow_twice_produces_independent_task_sets(db):
    wf = _make_workflow(db)
    persistence.save_workflow_definition(
        db,
        workflow_id=wf["id"],
        tasks=[{"client_id": "a", "name": "solo", "task_type": "echo"}],
        dependencies=[],
    )

    run1 = persistence.create_workflow_run(
        db, workflow_id=wf["id"], triggered_by_user_id=db.test_user_id
    )
    run2 = persistence.create_workflow_run(
        db, workflow_id=wf["id"], triggered_by_user_id=db.test_user_id
    )
    assert run1["id"] != run2["id"]

    tasks1 = persistence.list_tasks_for_run(db, run_id=run1["id"])
    tasks2 = persistence.list_tasks_for_run(db, run_id=run2["id"])
    assert len(tasks1) == 1 and len(tasks2) == 1
    assert tasks1[0].id != tasks2[0].id

    # Completing run1's task must not affect run2's independent rollup.
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    persistence.claim_task_by_id(
        db, task_id=tasks1[0].id, worker_id="w1", now=now, lease_duration=timedelta(minutes=5)
    )
    persistence.report_task_success(db, task_id=tasks1[0].id, worker_id="w1", now=now)

    counts1 = persistence.get_run_task_state_counts(db, run_id=run1["id"])
    counts2 = persistence.get_run_task_state_counts(db, run_id=run2["id"])
    assert counts1["succeeded"] == 1
    assert counts2["succeeded"] == 0
    assert counts2["ready"] == 1

    # The workflow-wide (all-runs) count still aggregates both.
    workflow_counts = persistence.get_workflow_task_state_counts(db, workflow_id=wf["id"])
    assert workflow_counts["total_tasks"] == 2


def test_direct_task_creation_still_has_null_run_columns(db):
    """Regression check: persistence.create_task (the pre-existing,
    non-Run creation path) is completely unaffected by the new nullable
    columns."""
    wf = _make_workflow(db)
    record = persistence.create_task(db, workflow_id=wf["id"], name="direct", task_type="echo")
    assert record.workflow_run_id is None
    assert record.task_definition_id is None
