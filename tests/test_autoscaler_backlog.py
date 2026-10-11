"""Autoscaling: the backlog signal against a real PostgreSQL. Verifies
persistence.get_backlog_counts counts only READY (backlog) and RUNNING,
and never counts terminal or PENDING tasks -- "do not count completed
historical tasks as backlog", and PENDING work isn't dispatchable so it
isn't backlog either."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from db import persistence

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)


def _wf(db):
    return persistence.create_workflow(db, name="backlog-wf", user_id=db.test_user_id)


def test_backlog_counts_ready_and_running_only(db):
    wf = _wf(db)

    # 8 echo tasks, all initially READY.
    echo_ids = [
        persistence.create_task(db, workflow_id=wf["id"], name=f"e{i}", task_type="echo").id
        for i in range(8)
    ]
    # 2 -> RUNNING (claimed, not reported)
    for tid in echo_ids[:2]:
        persistence.claim_task_by_id(db, task_id=tid, worker_id="w1", now=NOW, lease_duration=LEASE)
    # 1 -> SUCCEEDED (claim + report) : must NOT count as backlog
    persistence.claim_task_by_id(db, task_id=echo_ids[2], worker_id="w1", now=NOW, lease_duration=LEASE)
    persistence.report_task_success(db, task_id=echo_ids[2], worker_id="w1", now=NOW)

    # 3 sleep tasks, all READY.
    for i in range(3):
        persistence.create_task(db, workflow_id=wf["id"], name=f"s{i}", task_type="sleep")

    # A PENDING echo task (depends on a not-yet-succeeded task) -- not
    # dispatchable, so not backlog.
    persistence.create_task(
        db, workflow_id=wf["id"], name="blocked", task_type="echo",
        depends_on_ids=[echo_ids[0]],
    )

    counts = persistence.get_backlog_counts(db)

    # echo: 8 created, -2 running, -1 succeeded = 5 READY; +2 RUNNING; the
    # PENDING one is excluded from both.
    assert counts["echo"] == {"backlog": 5, "running": 2}
    assert counts["sleep"] == {"backlog": 3, "running": 0}


def test_backlog_absent_task_type_missing_from_result(db):
    wf = _wf(db)
    persistence.create_task(db, workflow_id=wf["id"], name="e", task_type="echo")
    counts = persistence.get_backlog_counts(db)
    assert "echo" in counts
    assert "http_request" not in counts  # no rows -> absent (autoscaler reads as 0)


def test_backlog_empty_database(db):
    assert persistence.get_backlog_counts(db) == {}
