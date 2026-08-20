"""Unit tests for core.transitions -- the Chronos task state machine."""

from datetime import datetime, timedelta, timezone

import pytest

from core.transitions import (
    TaskInstance,
    TaskState,
    TransitionError,
    claim,
    create_task,
    expire_lease,
    mark_ready,
    report_failure,
    report_success,
    skip,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)
NON_UTC = timezone(timedelta(hours=2))

ALL_STATES = list(TaskState)


def _terminal_instance(state: TaskState) -> TaskInstance:
    return TaskInstance(state=state, attempt_count=1, max_attempts=1)


# ---------------------------------------------------------------------------
# creation
# ---------------------------------------------------------------------------


def test_create_without_dependencies_is_ready():
    task = create_task(has_dependencies=False)
    assert task.state == TaskState.READY
    assert task.attempt_count == 0


def test_create_with_dependencies_is_pending():
    task = create_task(has_dependencies=True)
    assert task.state == TaskState.PENDING
    assert task.attempt_count == 0


def test_task_instance_has_no_has_dependencies_field():
    task = create_task(has_dependencies=True)
    assert not hasattr(task, "has_dependencies")


# ---------------------------------------------------------------------------
# PENDING -> READY
# ---------------------------------------------------------------------------


def test_pending_to_ready_when_dependencies_satisfied():
    task = create_task(has_dependencies=True)
    result = mark_ready(task, dependencies_satisfied=True)
    assert result.applied is True
    assert result.task.state == TaskState.READY


def test_pending_stays_pending_when_dependencies_not_satisfied():
    task = create_task(has_dependencies=True)
    result = mark_ready(task, dependencies_satisfied=False)
    assert result.applied is False
    assert result.task.state == TaskState.PENDING
    assert result.task is task  # unchanged instance returned


def test_mark_ready_is_a_no_op_when_already_ready():
    task = create_task(has_dependencies=False)  # READY
    result = mark_ready(task, dependencies_satisfied=True)
    assert result.applied is False
    assert result.task is task
    assert result.task.state == TaskState.READY


@pytest.mark.parametrize(
    "state",
    [TaskState.RUNNING, TaskState.SUCCEEDED, TaskState.FAILED_TERMINAL, TaskState.SKIPPED],
)
def test_mark_ready_rejects_every_other_invalid_source_state(state):
    task = _terminal_instance(state) if state != TaskState.RUNNING else TaskInstance(
        state=TaskState.RUNNING, attempt_count=1, max_attempts=1, lease_owner="w1", lease_expires_at=NOW
    )
    with pytest.raises(TransitionError):
        mark_ready(task, dependencies_satisfied=True)


# ---------------------------------------------------------------------------
# PENDING cannot go directly to RUNNING (no hop-skipping)
# ---------------------------------------------------------------------------


def test_pending_cannot_be_claimed_directly():
    task = create_task(has_dependencies=True)  # PENDING
    result = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE)
    assert result.applied is False
    assert result.task.state == TaskState.PENDING


# ---------------------------------------------------------------------------
# READY -> RUNNING (claim)
# ---------------------------------------------------------------------------


def test_ready_to_running_on_claim_sets_lease_and_increments_attempt():
    task = create_task(has_dependencies=False, max_attempts=3)
    result = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE)
    assert result.applied is True
    running = result.task
    assert running.state == TaskState.RUNNING
    assert running.attempt_count == 1
    assert running.lease_owner == "w1"
    assert running.lease_expires_at == NOW + LEASE


def test_claim_loses_race_when_already_claimed():
    task = create_task(has_dependencies=False)
    first = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    result = claim(first, worker_id="w2", now=NOW, lease_duration=LEASE)
    assert result.applied is False
    assert result.task.lease_owner == "w1"  # unchanged


def test_claim_requires_worker_id():
    task = create_task(has_dependencies=False)
    with pytest.raises(TransitionError):
        claim(task, worker_id="", now=NOW, lease_duration=LEASE)


# ---------------------------------------------------------------------------
# RUNNING -> SUCCEEDED
# ---------------------------------------------------------------------------


def test_running_to_succeeded_on_valid_report():
    task = create_task(has_dependencies=False)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    result = report_success(running, worker_id="w1", now=NOW)
    assert result.applied is True
    assert result.task.state == TaskState.SUCCEEDED
    assert result.task.lease_owner is None
    assert result.task.lease_expires_at is None


# ---------------------------------------------------------------------------
# RUNNING -> READY (retry) vs RUNNING -> FAILED_TERMINAL (exhausted)
# ---------------------------------------------------------------------------


def test_running_to_ready_on_failure_when_attempts_remain():
    task = create_task(has_dependencies=False, max_attempts=3)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    result = report_failure(running, worker_id="w1", now=NOW)
    assert result.applied is True
    assert result.task.state == TaskState.READY
    assert result.task.attempt_count == 1
    assert result.task.lease_owner is None


def test_running_to_failed_terminal_when_attempts_exhausted():
    task = create_task(has_dependencies=False, max_attempts=1)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    result = report_failure(running, worker_id="w1", now=NOW)
    assert result.applied is True
    assert result.task.state == TaskState.FAILED_TERMINAL
    assert result.task.attempt_count == 1


def test_retry_cycle_then_exhaustion():
    task = create_task(has_dependencies=False, max_attempts=2)

    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    after_fail_1 = report_failure(running, worker_id="w1", now=NOW).task
    assert after_fail_1.state == TaskState.READY
    assert after_fail_1.attempt_count == 1

    running_2 = claim(after_fail_1, worker_id="w2", now=NOW, lease_duration=LEASE).task
    assert running_2.attempt_count == 2
    after_fail_2 = report_failure(running_2, worker_id="w2", now=NOW).task
    assert after_fail_2.state == TaskState.FAILED_TERMINAL
    assert after_fail_2.attempt_count == 2


def test_duplicate_non_terminal_failure_report_is_ignored():
    """After RUNNING->READY (attempts remain), a stale second failure
    report from the same worker must not re-apply -- the lease is already
    cleared, so the task is no longer RUNNING."""
    task = create_task(has_dependencies=False, max_attempts=3)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    ready_again = report_failure(running, worker_id="w1", now=NOW).task
    assert ready_again.state == TaskState.READY

    replay = report_failure(ready_again, worker_id="w1", now=NOW)
    assert replay.applied is False
    assert replay.task.state == TaskState.READY
    assert replay.task is ready_again


def test_duplicate_non_terminal_success_report_is_ignored():
    task = create_task(has_dependencies=False, max_attempts=3)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    ready_again = report_failure(running, worker_id="w1", now=NOW).task

    replay = report_success(ready_again, worker_id="w1", now=NOW)
    assert replay.applied is False
    assert replay.task.state == TaskState.READY


# ---------------------------------------------------------------------------
# reports against never-claimed tasks (PENDING / READY)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("has_deps", [True, False])
def test_report_success_against_never_claimed_task_is_ignored(has_deps):
    task = create_task(has_dependencies=has_deps)  # PENDING or READY
    result = report_success(task, worker_id="w1", now=NOW)
    assert result.applied is False
    assert result.task is task


@pytest.mark.parametrize("has_deps", [True, False])
def test_report_failure_against_never_claimed_task_is_ignored(has_deps):
    task = create_task(has_dependencies=has_deps)  # PENDING or READY
    result = report_failure(task, worker_id="w1", now=NOW)
    assert result.applied is False
    assert result.task is task


# ---------------------------------------------------------------------------
# attempt_count boundaries
# ---------------------------------------------------------------------------


def test_attempt_count_never_exceeds_max_attempts():
    with pytest.raises(ValueError):
        TaskInstance(state=TaskState.READY, attempt_count=2, max_attempts=1)


def test_attempt_count_incremented_exactly_once_per_claim():
    task = create_task(has_dependencies=False, max_attempts=5)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    assert running.attempt_count == 1
    # success report does not touch attempt_count
    done = report_success(running, worker_id="w1", now=NOW).task
    assert done.attempt_count == 1


def test_claim_refuses_when_attempts_already_exhausted():
    # Construct a READY task that is (abnormally) already at its cap, to
    # verify the defensive guard in claim().
    task = TaskInstance(state=TaskState.READY, attempt_count=1, max_attempts=1)
    result = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE)
    assert result.applied is False
    assert result.task.state == TaskState.READY


# ---------------------------------------------------------------------------
# terminal-state immutability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "terminal_state",
    [TaskState.SUCCEEDED, TaskState.FAILED_TERMINAL, TaskState.SKIPPED],
)
def test_terminal_states_reject_further_claims(terminal_state):
    task = _terminal_instance(terminal_state)
    result = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE)
    assert result.applied is False
    assert result.task.state == terminal_state


def test_succeeded_task_ignores_further_success_reports():
    task = create_task(has_dependencies=False)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    done = report_success(running, worker_id="w1", now=NOW).task
    assert done.state == TaskState.SUCCEEDED

    # A duplicate/late report against the now-terminal task is ignored, not
    # an error, and does not change anything.
    replay = report_success(done, worker_id="w1", now=NOW)
    assert replay.applied is False
    assert replay.task.state == TaskState.SUCCEEDED


def test_failed_terminal_task_ignores_failure_reports():
    task = create_task(has_dependencies=False, max_attempts=1)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    terminal = report_failure(running, worker_id="w1", now=NOW).task
    assert terminal.state == TaskState.FAILED_TERMINAL

    replay = report_failure(terminal, worker_id="w1", now=NOW)
    assert replay.applied is False
    assert replay.task.state == TaskState.FAILED_TERMINAL


def test_skip_rejects_already_terminal_non_skipped_states():
    task = _terminal_instance(TaskState.SUCCEEDED)
    with pytest.raises(TransitionError):
        skip(task)


@pytest.mark.parametrize(
    "state",
    [TaskState.READY, TaskState.RUNNING, TaskState.SUCCEEDED, TaskState.FAILED_TERMINAL],
)
def test_skip_rejects_every_invalid_source_state(state):
    if state == TaskState.RUNNING:
        task = TaskInstance(
            state=TaskState.RUNNING, attempt_count=1, max_attempts=1, lease_owner="w1", lease_expires_at=NOW
        )
    elif state == TaskState.READY:
        task = create_task(has_dependencies=False)
    else:
        task = _terminal_instance(state)
    with pytest.raises(TransitionError):
        skip(task)


def test_skip_is_a_no_op_when_already_skipped():
    task = _terminal_instance(TaskState.SKIPPED)
    result = skip(task)
    assert result.applied is False
    assert result.task is task
    assert result.task.state == TaskState.SKIPPED


# ---------------------------------------------------------------------------
# stale / zombie worker fencing
# ---------------------------------------------------------------------------


def test_stale_worker_success_report_is_ignored():
    task = create_task(has_dependencies=False, max_attempts=2)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    after_fail = report_failure(running, worker_id="w1", now=NOW).task  # -> READY
    running_2 = claim(after_fail, worker_id="w2", now=NOW, lease_duration=LEASE).task

    # w1's zombie report arrives after w2 has already claimed the retry.
    result = report_success(running_2, worker_id="w1", now=NOW)
    assert result.applied is False
    assert result.task.state == TaskState.RUNNING
    assert result.task.lease_owner == "w2"


def test_expired_lease_report_is_ignored_even_from_owner():
    task = create_task(has_dependencies=False)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    late = NOW + LEASE + timedelta(seconds=1)
    result = report_success(running, worker_id="w1", now=late)
    assert result.applied is False
    assert result.task.state == TaskState.RUNNING


def test_report_at_exact_lease_expiry_boundary_is_still_valid():
    task = create_task(has_dependencies=False)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    result = report_success(running, worker_id="w1", now=NOW + LEASE)  # now == lease_expires_at
    assert result.applied is True
    assert result.task.state == TaskState.SUCCEEDED


def test_wrong_worker_failure_report_is_ignored():
    task = create_task(has_dependencies=False)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    result = report_failure(running, worker_id="intruder", now=NOW)
    assert result.applied is False
    assert result.task.state == TaskState.RUNNING
    assert result.task.lease_owner == "w1"


# ---------------------------------------------------------------------------
# expire_lease (reaper entry point)
# ---------------------------------------------------------------------------


def test_expire_lease_to_ready_when_attempts_remain():
    task = create_task(has_dependencies=False, max_attempts=3)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    after_expiry = NOW + LEASE + timedelta(seconds=1)
    result = expire_lease(running, now=after_expiry)
    assert result.applied is True
    assert result.task.state == TaskState.READY
    assert result.task.attempt_count == 1
    assert result.task.lease_owner is None
    assert result.task.lease_expires_at is None


def test_expire_lease_to_failed_terminal_when_attempts_exhausted():
    task = create_task(has_dependencies=False, max_attempts=1)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    after_expiry = NOW + LEASE + timedelta(seconds=1)
    result = expire_lease(running, now=after_expiry)
    assert result.applied is True
    assert result.task.state == TaskState.FAILED_TERMINAL


def test_expire_lease_rejects_non_expired_lease():
    task = create_task(has_dependencies=False)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    with pytest.raises(TransitionError):
        expire_lease(running, now=NOW)  # lease not yet expired


def test_expire_lease_rejects_lease_at_exact_boundary():
    # now == lease_expires_at is NOT expired (strictly earlier required).
    task = create_task(has_dependencies=False)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    with pytest.raises(TransitionError):
        expire_lease(running, now=NOW + LEASE)


def test_expire_lease_rejects_non_running_task():
    task = create_task(has_dependencies=False)  # READY, no lease
    with pytest.raises(TransitionError):
        expire_lease(task, now=NOW + LEASE)


def test_expire_lease_takes_no_worker_id_parameter():
    import inspect

    params = inspect.signature(expire_lease).parameters
    assert "worker_id" not in params


def test_expire_lease_does_not_bypass_worker_fencing_for_completion():
    """expire_lease is a distinct decision path from report_success/failure;
    it must never be usable to accept work on behalf of a specific worker."""
    task = create_task(has_dependencies=False, max_attempts=3)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    after_expiry = NOW + LEASE + timedelta(seconds=1)

    # Once expired, w1's own success report must still be ignored --
    # expire_lease does not retroactively validate it.
    stale_success = report_success(running, worker_id="w1", now=after_expiry)
    assert stale_success.applied is False
    assert running.state == TaskState.RUNNING  # original untouched


# ---------------------------------------------------------------------------
# SKIPPED cascade
# ---------------------------------------------------------------------------


def test_pending_to_skipped_via_cascade():
    task = create_task(has_dependencies=True)
    result = skip(task)
    assert result.applied is True
    assert result.task.state == TaskState.SKIPPED


def test_skip_cascade_is_idempotent():
    task = create_task(has_dependencies=True)
    once = skip(task).task
    assert once.state == TaskState.SKIPPED

    twice = skip(once)
    assert twice.applied is False
    assert twice.task.state == TaskState.SKIPPED
    assert twice.task is once  # no new instance churned out


def test_ready_task_cannot_be_skipped():
    task = create_task(has_dependencies=False)  # READY
    with pytest.raises(TransitionError):
        skip(task)


def test_running_task_cannot_be_skipped():
    task = create_task(has_dependencies=False)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    with pytest.raises(TransitionError):
        skip(running)


# ---------------------------------------------------------------------------
# lease_owner / lease_expires_at pairing invariant
# ---------------------------------------------------------------------------


def test_lease_owner_without_expiry_is_rejected():
    with pytest.raises(ValueError):
        TaskInstance(state=TaskState.RUNNING, lease_owner="w1", lease_expires_at=None)


def test_lease_expiry_without_owner_is_rejected():
    with pytest.raises(ValueError):
        TaskInstance(
            state=TaskState.RUNNING, lease_owner=None, lease_expires_at=NOW
        )


def test_non_running_task_cannot_hold_a_lease():
    with pytest.raises(ValueError):
        TaskInstance(state=TaskState.READY, lease_owner="w1", lease_expires_at=NOW)


def test_running_task_must_hold_a_lease():
    with pytest.raises(ValueError):
        TaskInstance(state=TaskState.RUNNING, lease_owner=None, lease_expires_at=None)


# ---------------------------------------------------------------------------
# timezone-aware UTC enforcement
# ---------------------------------------------------------------------------


NAIVE_NOW = datetime(2026, 1, 1, 12, 0, 0)  # no tzinfo


def test_claim_rejects_naive_datetime():
    task = create_task(has_dependencies=False)
    with pytest.raises(TransitionError):
        claim(task, worker_id="w1", now=NAIVE_NOW, lease_duration=LEASE)


def test_report_success_rejects_naive_datetime():
    task = create_task(has_dependencies=False)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    with pytest.raises(TransitionError):
        report_success(running, worker_id="w1", now=NAIVE_NOW)


def test_report_failure_rejects_naive_datetime():
    task = create_task(has_dependencies=False)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    with pytest.raises(TransitionError):
        report_failure(running, worker_id="w1", now=NAIVE_NOW)


def test_expire_lease_rejects_naive_datetime():
    task = create_task(has_dependencies=False)
    running = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE).task
    with pytest.raises(TransitionError):
        expire_lease(running, now=NAIVE_NOW)


def test_non_utc_aware_datetime_is_rejected():
    task = create_task(has_dependencies=False)
    non_utc_now = datetime(2026, 1, 1, 14, 0, 0, tzinfo=NON_UTC)
    with pytest.raises(TransitionError):
        claim(task, worker_id="w1", now=non_utc_now, lease_duration=LEASE)


def test_task_instance_rejects_naive_lease_expires_at():
    with pytest.raises(TransitionError):
        TaskInstance(
            state=TaskState.RUNNING,
            attempt_count=1,
            max_attempts=1,
            lease_owner="w1",
            lease_expires_at=NAIVE_NOW,
        )


def test_aware_utc_datetime_works_end_to_end():
    task = create_task(has_dependencies=False, max_attempts=2)
    result = claim(task, worker_id="w1", now=NOW, lease_duration=LEASE)
    assert result.applied is True
    assert result.task.lease_expires_at.tzinfo is not None
    assert result.task.lease_expires_at.utcoffset() == timedelta(0)
