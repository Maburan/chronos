"""Phase 4: dispatch-hint message validation tests. Pure parsing logic,
no Postgres, no RabbitMQ."""

from __future__ import annotations

import json

import pytest

from worker.message import DispatchHint, InvalidDispatchHintError, parse_dispatch_hint


def _body(**fields) -> bytes:
    return json.dumps(fields).encode("utf-8")


def test_parses_valid_dispatch_hint():
    hint = parse_dispatch_hint(
        _body(event_id=1, task_id=42, workflow_id=7, task_type="send_email")
    )
    assert hint == DispatchHint(event_id=1, task_id=42, workflow_id=7, task_type="send_email")


def test_rejects_non_json_body():
    with pytest.raises(InvalidDispatchHintError):
        parse_dispatch_hint(b"not json at all {{{")


def test_rejects_json_array_body():
    with pytest.raises(InvalidDispatchHintError):
        parse_dispatch_hint(b"[1, 2, 3]")


def test_rejects_json_scalar_body():
    with pytest.raises(InvalidDispatchHintError):
        parse_dispatch_hint(b"42")


@pytest.mark.parametrize("missing", ["event_id", "task_id", "workflow_id", "task_type"])
def test_rejects_missing_field(missing):
    fields = {"event_id": 1, "task_id": 42, "workflow_id": 7, "task_type": "send_email"}
    del fields[missing]
    with pytest.raises(InvalidDispatchHintError):
        parse_dispatch_hint(_body(**fields))


def test_rejects_non_integer_task_id():
    with pytest.raises(InvalidDispatchHintError):
        parse_dispatch_hint(
            _body(event_id=1, task_id="42", workflow_id=7, task_type="send_email")
        )


def test_rejects_boolean_as_integer_field():
    """bool is technically an int subclass in Python -- must not be
    silently accepted as 0/1."""
    with pytest.raises(InvalidDispatchHintError):
        parse_dispatch_hint(
            _body(event_id=1, task_id=True, workflow_id=7, task_type="send_email")
        )


def test_rejects_non_string_task_type():
    with pytest.raises(InvalidDispatchHintError):
        parse_dispatch_hint(_body(event_id=1, task_id=42, workflow_id=7, task_type=123))


def test_rejects_empty_string_task_type():
    with pytest.raises(InvalidDispatchHintError):
        parse_dispatch_hint(_body(event_id=1, task_id=42, workflow_id=7, task_type=""))


def test_rejects_malformed_utf8():
    with pytest.raises(InvalidDispatchHintError):
        parse_dispatch_hint(b"\xff\xfe\x00\x01not utf8")
