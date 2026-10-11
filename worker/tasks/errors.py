"""Shared exception types for the task types in this package. Lives in
its own module (rather than ``worker/tasks/__init__.py``) purely to avoid
a circular import: the package ``__init__`` imports every task module,
and every task module needs these types.
"""

from __future__ import annotations


class TaskExecutionError(Exception):
    """Common base for every validation/execution failure raised by a
    task type in this package. Not caught specially anywhere -- any
    exception at all already flows into the ordinary retry/terminal
    decision (see worker/execution.py::execute_and_report); this base
    class exists purely so a caller can catch "one of ours" if it ever
    needs to, without changing that behavior."""


class InvalidTaskPayloadError(TaskExecutionError):
    """The task's payload failed this task type's own validation (missing
    /wrong-typed/out-of-range field, disallowed expression syntax, etc.)
    -- never a Chronos orchestration bug."""


class SsrfBlockedError(TaskExecutionError):
    """The ``http_request`` task type refused to contact a resolved
    address because it falls in a private/internal/loopback range (see
    ``worker/tasks/http_task.py``)."""
