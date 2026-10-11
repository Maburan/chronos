"""API tests for GET /task-types -- static task-type metadata used by the
frontend's workflow-creation task-type picker (see worker/tasks/__init__.py's
TASK_TYPE_METADATA, the single source of truth this route reads from)."""

from __future__ import annotations

from worker.tasks import TASK_TYPE_REGISTRY


def test_list_task_types_requires_no_authentication(api_app):
    response = api_app.get("/task-types")
    assert response.status_code == 200


def test_list_task_types_returns_every_registered_type(api_app):
    response = api_app.get("/task-types")

    body = response.json()
    types = {t["type"] for t in body}
    assert types == set(TASK_TYPE_REGISTRY)


def test_list_task_types_entries_have_the_expected_shape(api_app):
    response = api_app.get("/task-types")

    for entry in response.json():
        assert isinstance(entry["type"], str) and entry["type"]
        assert isinstance(entry["label"], str) and entry["label"]
        assert isinstance(entry["description"], str) and entry["description"]
        assert isinstance(entry["fields"], list)
        for field in entry["fields"]:
            assert isinstance(field["name"], str)
            assert isinstance(field["type"], str)
            assert isinstance(field["required"], bool)
