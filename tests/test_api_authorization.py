"""Authorization/ownership isolation tests: user A must never be able to
read or mutate user B's workflows/tasks through the API, and every
protected route must reject an unauthenticated caller. Uses the
unauthenticated ``api_app`` fixture directly (not ``api_client``) since
these tests need two distinct, independently-authenticated identities."""

from __future__ import annotations

import pytest


def _register_and_login(api_app, email, password="a-decent-password"):
    register = api_app.post("/auth/register", json={"email": email, "password": password})
    assert register.status_code == 201, register.text
    login = api_app.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def two_users(api_app):
    token_a = _register_and_login(api_app, "owner-a@example.com")
    token_b = _register_and_login(api_app, "owner-b@example.com")
    return token_a, token_b


# ---------------------------------------------------------------------------
# unauthenticated requests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/workflows"),
        ("GET", "/workflows"),
        ("GET", "/workflows/1"),
        ("POST", "/workflows/1/tasks"),
        ("GET", "/workflows/1/tasks"),
        ("GET", "/workflows/1/status"),
        ("GET", "/tasks/1"),
    ],
)
def test_protected_routes_require_authentication(api_app, method, path):
    response = api_app.request(method, path, json={"name": "x"} if method == "POST" else None)
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# workflow ownership isolation
# ---------------------------------------------------------------------------


def test_user_cannot_get_another_users_workflow(api_app, two_users):
    token_a, token_b = two_users
    wf = api_app.post("/workflows", json={"name": "a-private-workflow"}, headers=_auth(token_a)).json()

    response = api_app.get(f"/workflows/{wf['id']}", headers=_auth(token_b))

    assert response.status_code == 404


def test_user_cannot_list_another_users_workflow_tasks(api_app, two_users):
    token_a, token_b = two_users
    wf = api_app.post("/workflows", json={"name": "wf"}, headers=_auth(token_a)).json()
    api_app.post(
        f"/workflows/{wf['id']}/tasks",
        json={"name": "t1", "task_type": "noop"},
        headers=_auth(token_a),
    )

    response = api_app.get(f"/workflows/{wf['id']}/tasks", headers=_auth(token_b))

    assert response.status_code == 404


def test_user_cannot_create_a_task_on_another_users_workflow(api_app, two_users):
    token_a, token_b = two_users
    wf = api_app.post("/workflows", json={"name": "wf"}, headers=_auth(token_a)).json()

    response = api_app.post(
        f"/workflows/{wf['id']}/tasks",
        json={"name": "intruding-task", "task_type": "noop"},
        headers=_auth(token_b),
    )

    assert response.status_code == 404


def test_user_cannot_get_another_users_workflow_status(api_app, two_users):
    token_a, token_b = two_users
    wf = api_app.post("/workflows", json={"name": "wf"}, headers=_auth(token_a)).json()

    response = api_app.get(f"/workflows/{wf['id']}/status", headers=_auth(token_b))

    assert response.status_code == 404


def test_user_cannot_get_a_task_belonging_to_another_users_workflow(api_app, two_users):
    token_a, token_b = two_users
    wf = api_app.post("/workflows", json={"name": "wf"}, headers=_auth(token_a)).json()
    task = api_app.post(
        f"/workflows/{wf['id']}/tasks",
        json={"name": "t1", "task_type": "noop"},
        headers=_auth(token_a),
    ).json()

    response = api_app.get(f"/tasks/{task['id']}", headers=_auth(token_b))

    assert response.status_code == 404


def test_list_workflows_only_returns_the_caller_s_own(api_app, two_users):
    token_a, token_b = two_users
    api_app.post("/workflows", json={"name": "a1"}, headers=_auth(token_a))
    api_app.post("/workflows", json={"name": "a2"}, headers=_auth(token_a))
    api_app.post("/workflows", json={"name": "b1"}, headers=_auth(token_b))

    response_a = api_app.get("/workflows", headers=_auth(token_a))
    response_b = api_app.get("/workflows", headers=_auth(token_b))

    assert {w["name"] for w in response_a.json()} == {"a1", "a2"}
    assert {w["name"] for w in response_b.json()} == {"b1"}


def test_owner_can_still_access_their_own_workflow(api_app, two_users):
    """Sanity check alongside the isolation tests above: the ownership
    check must reject the wrong user without also rejecting the right
    one."""
    token_a, _ = two_users
    wf = api_app.post("/workflows", json={"name": "wf"}, headers=_auth(token_a)).json()

    response = api_app.get(f"/workflows/{wf['id']}", headers=_auth(token_a))

    assert response.status_code == 200
