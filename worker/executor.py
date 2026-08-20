"""The execution abstraction: the smallest possible
``task_type -> callable`` registry.

Deliberately NOT a plugin framework, NOT a subprocess/sandboxed execution
engine, NOT dynamic-import task discovery, NOT remote execution -- just a
dict lookup plus one function call, injected by whoever constructs the
worker. If Chronos ever needs any of those things, that's a decision for
a later phase, not something to grow here by default.
"""

from __future__ import annotations

from typing import Callable, Mapping

from db.models import TaskRecord

TaskExecutorFn = Callable[[TaskRecord], None]
"""A registered task_type's execution function: receives the claimed,
authoritative ``TaskRecord`` (the only source of task facts trusted past
the claim -- see worker/message.py's module docstring) and either returns
normally (success) or raises (failure, any exception type). No return
value is interpreted."""


class UnregisteredTaskTypeError(Exception):
    """Raised when a claimed task's ``task_type`` has no registered
    executor. Treated by the worker exactly like any other execution
    failure (see worker/task_worker.py) -- this flows into
    ``report_task_failure`` and the existing retry/terminal decision
    already owned by core/transitions.py. No new task state is invented
    for "unregistered task_type"."""


class TaskExecutorRegistry:
    """Looks up and invokes the callable registered for a claimed
    ``TaskRecord``'s ``task_type``. Constructed once by whoever wires up a
    worker process (e.g. ``TaskExecutorRegistry({"send_email": send_email_fn})``)
    and handed to ``worker.task_worker.TaskWorker``.
    """

    def __init__(self, executors: Mapping[str, TaskExecutorFn]) -> None:
        self._executors = dict(executors)

    def execute(self, record: TaskRecord) -> None:
        executor = self._executors.get(record.task_type)
        if executor is None:
            raise UnregisteredTaskTypeError(
                f"no executor registered for task_type={record.task_type!r}"
            )
        executor(record)
