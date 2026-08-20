"""Phase 8: API tests for the workflow endpoints. Real PostgreSQL
throughout (via the ``api_client`` fixture's own real connection pool --
see tests/conftest.py) -- no mocking of the database anywhere."""

from __future__ import annotations


def test_create_workflow_returns_201_and_shape(api_client):
    response = api_client.post("/workflows", json={"name": "demo"})

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "demo"
    assert isinstance(body["id"], int)
    assert "created_at" in body


def test_get_workflow_found(api_client):
    created = api_client.post("/workflows", json={"name": "wf1"}).json()

    response = api_client.get(f"/workflows/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_get_workflow_not_found_returns_404(api_client):
    response = api_client.get("/workflows/999999")
    assert response.status_code == 404


def test_get_workflow_invalid_id_returns_422(api_client):
    """Path parameter type coercion (workflow_id: int) rejects a
    non-integer id before any persistence call is ever made."""
    response = api_client.get("/workflows/not-an-integer")
    assert response.status_code == 422


def test_create_workflow_missing_name_returns_422(api_client):
    response = api_client.post("/workflows", json={})
    assert response.status_code == 422


def test_create_workflow_malformed_json_returns_422(api_client):
    response = api_client.post(
        "/workflows", content=b"{not valid json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 422


def test_create_workflow_wrong_type_for_name_returns_422(api_client):
    response = api_client.post("/workflows", json={"name": 12345})
    assert response.status_code == 422
