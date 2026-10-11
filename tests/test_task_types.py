"""Unit tests for the task types registered in worker/tasks/ (sleep,
http_request, python_expression). No database/broker required -- these
call each handler function directly against a hand-built ``TaskRecord``,
same as how ``worker.executor.TaskExecutorRegistry.execute`` would call
it after a real claim. The original ``echo`` task type
(worker/demo_task.py) is untouched and already covered by
tests/test_end_to_end.py and tests/test_crash_recovery.py.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from core.transitions import TaskInstance, TaskState
from db.models import TaskRecord
from worker.tasks import TASK_TYPE_METADATA, TASK_TYPE_REGISTRY
from worker.tasks.errors import InvalidTaskPayloadError, SsrfBlockedError
from worker.tasks.http_task import http_request_task
from worker.tasks.python_task import python_expression_task
from worker.tasks.sleep_task import sleep_task


def test_every_registered_task_type_has_metadata():
    """Pins that TASK_TYPE_REGISTRY (what the worker can execute) and
    TASK_TYPE_METADATA (what GET /task-types tells the frontend) never
    drift apart -- see worker/tasks/__init__.py."""
    assert set(TASK_TYPE_REGISTRY) == set(TASK_TYPE_METADATA)
    for task_type, info in TASK_TYPE_METADATA.items():
        assert info["label"]
        assert info["description"]
        assert isinstance(info["fields"], list)
        for field in info["fields"]:
            assert {"name", "type", "required"} <= set(field)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_record(*, task_type: str, payload: dict | None) -> TaskRecord:
    return TaskRecord(
        id=1,
        workflow_id=1,
        name="t",
        task_type=task_type,
        payload=payload,
        created_at=NOW,
        updated_at=NOW,
        instance=TaskInstance(
            state=TaskState.RUNNING,
            attempt_count=1,
            max_attempts=1,
            lease_owner="test-worker",
            lease_expires_at=NOW,
        ),
    )


# ---------------------------------------------------------------------------
# sleep
# ---------------------------------------------------------------------------


def test_sleep_task_succeeds_for_a_small_duration():
    record = _make_record(task_type="sleep", payload={"seconds": 0})
    sleep_task(record)  # must not raise


def test_sleep_task_rejects_missing_seconds():
    record = _make_record(task_type="sleep", payload={})
    with pytest.raises(InvalidTaskPayloadError):
        sleep_task(record)


def test_sleep_task_rejects_negative_seconds():
    record = _make_record(task_type="sleep", payload={"seconds": -1})
    with pytest.raises(InvalidTaskPayloadError):
        sleep_task(record)


def test_sleep_task_rejects_excessive_duration():
    record = _make_record(task_type="sleep", payload={"seconds": 10_000})
    with pytest.raises(InvalidTaskPayloadError):
        sleep_task(record)


def test_sleep_task_rejects_non_numeric_seconds():
    record = _make_record(task_type="sleep", payload={"seconds": "soon"})
    with pytest.raises(InvalidTaskPayloadError):
        sleep_task(record)


# ---------------------------------------------------------------------------
# http_request
# ---------------------------------------------------------------------------


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib naming convention
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # silence stdlib default logging to stderr
        pass


@pytest.fixture()
def local_http_server():
    server = HTTPServer(("127.0.0.1", 0), _OkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_http_request_task_blocks_loopback_by_default(local_http_server, monkeypatch):
    monkeypatch.delenv("CHRONOS_HTTP_TASK_ALLOW_PRIVATE_HOSTS", raising=False)
    record = _make_record(task_type="http_request", payload={"url": local_http_server})
    with pytest.raises(SsrfBlockedError):
        http_request_task(record)


def test_http_request_task_succeeds_when_private_hosts_are_explicitly_allowed(
    local_http_server, monkeypatch
):
    monkeypatch.setenv("CHRONOS_HTTP_TASK_ALLOW_PRIVATE_HOSTS", "1")
    record = _make_record(task_type="http_request", payload={"url": local_http_server})
    http_request_task(record)  # must not raise


def test_http_request_task_rejects_missing_url():
    record = _make_record(task_type="http_request", payload={})
    with pytest.raises(InvalidTaskPayloadError):
        http_request_task(record)


def test_http_request_task_rejects_non_http_scheme():
    record = _make_record(task_type="http_request", payload={"url": "file:///etc/passwd"})
    with pytest.raises(InvalidTaskPayloadError):
        http_request_task(record)


def test_http_request_task_rejects_metadata_style_address(monkeypatch):
    monkeypatch.delenv("CHRONOS_HTTP_TASK_ALLOW_PRIVATE_HOSTS", raising=False)
    record = _make_record(
        task_type="http_request", payload={"url": "http://169.254.169.254/latest/meta-data/"}
    )
    with pytest.raises(SsrfBlockedError):
        http_request_task(record)


def test_http_request_task_rejects_invalid_timeout(local_http_server, monkeypatch):
    monkeypatch.setenv("CHRONOS_HTTP_TASK_ALLOW_PRIVATE_HOSTS", "1")
    record = _make_record(
        task_type="http_request", payload={"url": local_http_server, "timeout": -5}
    )
    with pytest.raises(InvalidTaskPayloadError):
        http_request_task(record)


# ---------------------------------------------------------------------------
# python_expression
# ---------------------------------------------------------------------------


def test_python_expression_task_computes_arithmetic():
    record = _make_record(
        task_type="python_expression",
        payload={"expression": "a + b * 2", "variables": {"a": 1, "b": 3}},
    )
    python_expression_task(record)  # must not raise; result only logged


def test_python_expression_task_rejects_missing_expression():
    record = _make_record(task_type="python_expression", payload={})
    with pytest.raises(InvalidTaskPayloadError):
        python_expression_task(record)


def test_python_expression_task_rejects_unknown_variable():
    record = _make_record(
        task_type="python_expression", payload={"expression": "a + 1", "variables": {}}
    )
    with pytest.raises(InvalidTaskPayloadError):
        python_expression_task(record)


def test_python_expression_task_rejects_attribute_access():
    record = _make_record(
        task_type="python_expression",
        payload={"expression": "().__class__", "variables": {}},
    )
    with pytest.raises(InvalidTaskPayloadError):
        python_expression_task(record)


def test_python_expression_task_rejects_import_syntax():
    record = _make_record(
        task_type="python_expression",
        payload={"expression": "__import__('os').system('echo hi')", "variables": {}},
    )
    with pytest.raises(InvalidTaskPayloadError):
        python_expression_task(record)


def test_python_expression_task_rejects_non_whitelisted_function_call():
    record = _make_record(
        task_type="python_expression",
        payload={"expression": "open('/etc/passwd')", "variables": {}},
    )
    with pytest.raises(InvalidTaskPayloadError):
        python_expression_task(record)


def test_python_expression_task_rejects_statement_syntax():
    record = _make_record(
        task_type="python_expression",
        payload={"expression": "import os", "variables": {}},
    )
    with pytest.raises(InvalidTaskPayloadError):
        python_expression_task(record)


def test_python_expression_task_allows_whitelisted_function():
    record = _make_record(
        task_type="python_expression",
        payload={"expression": "max(a, b)", "variables": {"a": 1, "b": 2}},
    )
    python_expression_task(record)  # must not raise


def test_python_expression_task_rejects_oversized_expression():
    record = _make_record(
        task_type="python_expression",
        payload={"expression": "1 + " * 200 + "1", "variables": {}},
    )
    with pytest.raises(InvalidTaskPayloadError):
        python_expression_task(record)
