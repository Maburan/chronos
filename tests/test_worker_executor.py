"""Phase 4: TaskExecutorRegistry tests. Pure logic, no Postgres, no
RabbitMQ -- uses a minimal stand-in for TaskRecord (only .task_type is
read by the registry)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from worker.executor import TaskExecutorRegistry, UnregisteredTaskTypeError


@dataclass
class _FakeRecord:
    task_type: str
    id: int = 1


def test_dispatches_to_registered_executor():
    calls = []
    registry = TaskExecutorRegistry({"noop": lambda record: calls.append(record)})

    record = _FakeRecord(task_type="noop")
    registry.execute(record)

    assert calls == [record]


def test_executor_receives_the_record_itself():
    received = []
    registry = TaskExecutorRegistry({"send_email": received.append})

    record = _FakeRecord(task_type="send_email", id=99)
    registry.execute(record)

    assert received[0] is record


def test_executor_exception_propagates():
    def boom(record):
        raise RuntimeError("execution failed")

    registry = TaskExecutorRegistry({"noop": boom})
    with pytest.raises(RuntimeError):
        registry.execute(_FakeRecord(task_type="noop"))


def test_unregistered_task_type_raises():
    registry = TaskExecutorRegistry({"noop": lambda record: None})
    with pytest.raises(UnregisteredTaskTypeError):
        registry.execute(_FakeRecord(task_type="unknown_type"))


def test_multiple_task_types_route_independently():
    calls = {"a": 0, "b": 0}
    registry = TaskExecutorRegistry(
        {
            "type_a": lambda record: calls.__setitem__("a", calls["a"] + 1),
            "type_b": lambda record: calls.__setitem__("b", calls["b"] + 1),
        }
    )

    registry.execute(_FakeRecord(task_type="type_a"))
    registry.execute(_FakeRecord(task_type="type_a"))
    registry.execute(_FakeRecord(task_type="type_b"))

    assert calls == {"a": 2, "b": 1}
