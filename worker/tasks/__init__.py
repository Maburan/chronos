"""Registered task types beyond the original ``echo`` demonstration task
(``worker/demo_task.py``, left in place unchanged -- existing tests
(``tests/test_end_to_end.py``, ``tests/test_crash_recovery.py``) and the
README's crash-recovery demo import it directly).

Each module here exposes exactly one ``(record: TaskRecord) -> None``
function plus its own ``*_TASK_TYPE`` string constant, matching
``worker.executor.TaskExecutorFn``'s contract -- there is no plugin
framework and no dynamic discovery here, just plain functions registered
by name in ``TASK_TYPE_REGISTRY`` below (see ``worker/executor.py``'s own
docstring for why that's deliberate). The orchestration engine
(core/transitions.py, db/persistence.py, worker/task_worker.py) has no
awareness of individual task types at all -- it only ever calls
``TaskExecutorRegistry.execute(record)``.

Every handler here raises a subclass of ``TaskExecutionError`` (or lets
one from a lower-level validation helper propagate) for a bad/unsafe
payload -- this is not a new failure channel, it flows into the same
report_task_failure/retry path any other exception already does (see
``worker/executor.py``'s ``UnregisteredTaskTypeError`` for the existing
precedent).

Explicitly excluded: a SQL query/read task type. In a multi-tenant
system, letting a task's payload (fully user-controlled, submitted over
the public HTTP API) run arbitrary SQL against a shared database
credential is a direct injection/data-exfiltration vector, and Chronos
has no per-tenant datasource abstraction to sandbox that against safely.
Left out per the project's own instruction to document a risky task type
rather than build it -- not an oversight.
"""

from __future__ import annotations

from worker.demo_task import ECHO_TASK_TYPE, echo_task
from worker.tasks.errors import InvalidTaskPayloadError, SsrfBlockedError, TaskExecutionError
from worker.tasks.http_task import HTTP_REQUEST_TASK_TYPE, http_request_task
from worker.tasks.python_task import PYTHON_EXPRESSION_TASK_TYPE, python_expression_task
from worker.tasks.sleep_task import SLEEP_TASK_TYPE, sleep_task

__all__ = [
    "TASK_TYPE_REGISTRY",
    "TaskExecutionError",
    "InvalidTaskPayloadError",
    "SsrfBlockedError",
]

TASK_TYPE_REGISTRY: dict = {
    ECHO_TASK_TYPE: echo_task,
    SLEEP_TASK_TYPE: sleep_task,
    HTTP_REQUEST_TASK_TYPE: http_request_task,
    PYTHON_EXPRESSION_TASK_TYPE: python_expression_task,
}

# Static, read-only description of each registered task type's expected
# payload shape -- the API's GET /task-types route (api/routes.py) serves
# this directly so the frontend can render a task-type picker/form
# without hardcoding field lists that could drift from what the worker
# actually accepts. This module is the single source of truth for both:
# tests/test_task_types.py::test_every_registered_task_type_has_metadata
# pins that TASK_TYPE_METADATA and TASK_TYPE_REGISTRY never disagree on
# which task types exist.
#
# Beyond the original name/type/required triple, every field below also
# carries the presentation metadata a generic frontend renderer needs to
# build a plain-language "Simple mode" form (and an "Advanced mode" one)
# for a task type it has never seen bespoke UI code for:
#
#   - category/icon (top level): which group and glyph a step picker
#     shows this type under.
#   - label/help_text/placeholder (per field): the human-facing copy.
#   - input_type (per field): which generic form control to render
#     (text/url/number/textarea/select/keyvalue/json/expression).
#   - advanced (per field): False = shown in Simple mode; True = shown
#     only once the user opens Advanced mode (retries, headers, timeout,
#     the echo demo's retry/crash-recovery knobs, etc.).
#   - validation (per field, optional): declarative bounds (min/max/
#     pattern) the frontend can enforce client-side; the worker's own
#     handler remains the actual authority (see worker/tasks/errors.py's
#     InvalidTaskPayloadError) -- this is a UX nicety, never a substitute
#     for server-side validation.
#
# Adding a new task type -- or a new field to an existing one -- is still
# a metadata + executor-function change only. No frontend code needs to
# change for a new type to render correctly, as long as its fields stick
# to the input_types already understood by the generic renderer.
TASK_TYPE_METADATA: dict = {
    ECHO_TASK_TYPE: {
        "label": "Echo (demo)",
        "description": (
            "Logs a message. Demonstration task type only -- also accepts "
            "fail_until_attempt/sleep_seconds/sleep_until_attempt knobs "
            "used to demo retries and crash recovery, not real task-type "
            "semantics."
        ),
        "category": "utility",
        "icon": "🔔",
        "fields": [
            {
                "name": "message",
                "type": "string",
                "required": False,
                "label": "Message",
                "help_text": "Text to log when this step runs.",
                "placeholder": "Hello, world",
                "input_type": "text",
                "advanced": False,
            },
            {
                "name": "fail_until_attempt",
                "type": "integer",
                "required": False,
                "label": "Fail until attempt",
                "help_text": "Demo knob: fail every attempt before this one, then succeed.",
                "input_type": "number",
                "advanced": True,
            },
            {
                "name": "sleep_seconds",
                "type": "number",
                "required": False,
                "label": "Sleep seconds",
                "help_text": "Demo knob: block for this long before finishing.",
                "input_type": "number",
                "advanced": True,
            },
            {
                "name": "sleep_until_attempt",
                "type": "integer",
                "required": False,
                "label": "Sleep until attempt",
                "help_text": "Demo knob: only sleep on attempts before this one.",
                "input_type": "number",
                "advanced": True,
            },
        ],
    },
    SLEEP_TASK_TYPE: {
        "label": "Wait",
        "description": "Pauses for a set amount of time before continuing.",
        "category": "utility",
        "icon": "⏱️",
        "fields": [
            {
                "name": "seconds",
                "type": "number",
                "required": True,
                "label": "Seconds to wait",
                "help_text": "How long to pause before moving on (max 300 seconds).",
                "placeholder": "5",
                "input_type": "number",
                "advanced": False,
                "validation": {"min": 0, "max": 300},
            },
        ],
    },
    HTTP_REQUEST_TASK_TYPE: {
        "label": "Get data from the web",
        "description": (
            "Fetches information from a website or web service. Blocks "
            "private/internal/loopback addresses by default (SSRF guard)."
        ),
        "category": "data",
        "icon": "🌐",
        "fields": [
            {
                "name": "url",
                "type": "string",
                "required": True,
                "label": "Web address",
                "help_text": "The URL to fetch data from.",
                "placeholder": "https://api.example.com/data",
                "input_type": "url",
                "advanced": False,
            },
            {
                "name": "method",
                "type": "string",
                "required": False,
                "default": "GET",
                "label": "Method",
                "help_text": "The kind of request to make.",
                "input_type": "select",
                "options": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                "advanced": False,
            },
            {
                "name": "headers",
                "type": "object",
                "required": False,
                "label": "Custom headers",
                "help_text": "Extra headers to send with the request.",
                "input_type": "keyvalue",
                "advanced": True,
            },
            {
                "name": "json",
                "type": "object",
                "required": False,
                "label": "Request body (JSON)",
                "help_text": "Data to send with the request, as JSON.",
                "input_type": "json",
                "advanced": True,
            },
            {
                "name": "timeout",
                "type": "number",
                "required": False,
                "default": 10,
                "label": "Timeout (seconds)",
                "help_text": "How long to wait for a response before giving up.",
                "input_type": "number",
                "advanced": True,
            },
        ],
    },
    PYTHON_EXPRESSION_TASK_TYPE: {
        "label": "Do a calculation",
        "description": (
            "Runs a simple formula on your data -- not general-purpose code "
            "(no imports, attribute access, or arbitrary function calls)."
        ),
        "category": "logic",
        "icon": "🧮",
        "fields": [
            {
                "name": "expression",
                "type": "string",
                "required": True,
                "label": "Formula",
                "help_text": "An arithmetic/comparison expression, e.g. a + b * 2.",
                "placeholder": "a + b * 2",
                "input_type": "expression",
                "advanced": False,
            },
            {
                "name": "variables",
                "type": "object",
                "required": False,
                "label": "Values",
                "help_text": "The named values your formula can use.",
                "input_type": "keyvalue",
                "advanced": False,
            },
        ],
    },
}
