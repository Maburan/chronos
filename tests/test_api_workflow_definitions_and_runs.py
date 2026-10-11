"""API tests for the Definition vs Run endpoints:

  PUT/GET /workflows/{id}/definition   -- Save/load the Builder graph
  POST    /workflows/{id}/runs         -- Run (real materialization)
  GET     /workflows/{id}/runs[...]    -- run history/status

Real PostgreSQL throughout (see tests/conftest.py). Mirrors
tests/test_api_tasks.py's style/ownership-check conventions.
"""

from __future__ import annotations


def _create_workflow(api_client, name="wf"):
    return api_client.post("/workflows", json={"name": name}).json()


def _register_and_login(api_app, email, password="a-decent-password"):
    register = api_app.post("/auth/register", json={"email": email, "password": password})
    assert register.status_code == 201, register.text
    login = api_app.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Save / load definition
# ---------------------------------------------------------------------------


def test_save_and_get_workflow_definition(api_client):
    wf = _create_workflow(api_client)

    save_resp = api_client.put(
        f"/workflows/{wf['id']}/definition",
        json={
            "tasks": [
                {"client_id": "a", "name": "fetch", "task_type": "http_request"},
                {"client_id": "b", "name": "notify", "task_type": "echo"},
            ],
            "dependencies": [{"task_client_id": "b", "depends_on_client_id": "a"}],
        },
    )
    assert save_resp.status_code == 200, save_resp.text
    body = save_resp.json()
    assert {t["name"] for t in body["tasks"]} == {"fetch", "notify"}

    # Saving a definition must never create a real, dispatchable task.
    tasks_resp = api_client.get(f"/workflows/{wf['id']}/tasks")
    assert tasks_resp.json() == []

    get_resp = api_client.get(f"/workflows/{wf['id']}/definition")
    assert get_resp.status_code == 200
    assert {t["name"] for t in get_resp.json()["tasks"]} == {"fetch", "notify"}


def test_save_workflow_definition_rejects_cycle(api_client):
    wf = _create_workflow(api_client)
    resp = api_client.put(
        f"/workflows/{wf['id']}/definition",
        json={
            "tasks": [
                {"client_id": "a", "name": "a", "task_type": "echo"},
                {"client_id": "b", "name": "b", "task_type": "echo"},
            ],
            "dependencies": [
                {"task_client_id": "a", "depends_on_client_id": "b"},
                {"task_client_id": "b", "depends_on_client_id": "a"},
            ],
        },
    )
    assert resp.status_code == 400


def test_definition_endpoints_for_nonexistent_workflow_return_404(api_client):
    assert api_client.get("/workflows/999999/definition").status_code == 404
    assert (
        api_client.put("/workflows/999999/definition", json={"tasks": []}).status_code
        == 404
    )


def test_definition_not_visible_to_a_different_user(api_app):
    token_owner = _register_and_login(api_app, "owner-def@example.com")
    token_other = _register_and_login(api_app, "other-def@example.com")

    wf = api_app.post(
        "/workflows", json={"name": "wf"}, headers=_auth(token_owner)
    ).json()
    api_app.put(
        f"/workflows/{wf['id']}/definition",
        json={"tasks": [{"client_id": "a", "name": "a", "task_type": "echo"}]},
        headers=_auth(token_owner),
    )

    resp = api_app.get(
        f"/workflows/{wf['id']}/definition", headers=_auth(token_other)
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Run -- the real materialization action
# ---------------------------------------------------------------------------


def test_run_endpoint_requires_a_saved_definition(api_client):
    wf = _create_workflow(api_client)
    resp = api_client.post(f"/workflows/{wf['id']}/runs")
    assert resp.status_code == 400


def test_run_endpoint_materializes_real_dispatchable_tasks(api_client):
    wf = _create_workflow(api_client)
    api_client.put(
        f"/workflows/{wf['id']}/definition",
        json={
            "tasks": [
                {"client_id": "a", "name": "fetch", "task_type": "http_request"},
                {"client_id": "b", "name": "notify", "task_type": "echo"},
            ],
            "dependencies": [{"task_client_id": "b", "depends_on_client_id": "a"}],
        },
    )

    # Before Run: no real tasks exist yet.
    assert api_client.get(f"/workflows/{wf['id']}/tasks").json() == []

    run_resp = api_client.post(f"/workflows/{wf['id']}/runs")
    assert run_resp.status_code == 201, run_resp.text
    run = run_resp.json()
    assert run["workflow_id"] == wf["id"]
    assert len(run["definition_snapshot"]["task_definitions"]) == 2

    # After Run: real tasks now exist, with correct initial states.
    tasks = api_client.get(f"/workflows/{wf['id']}/runs/{run['id']}/tasks").json()
    by_name = {t["name"]: t for t in tasks}
    assert by_name["fetch"]["state"] == "READY"
    assert by_name["notify"]["state"] == "PENDING"
    assert by_name["fetch"]["workflow_run_id"] == run["id"]

    status_resp = api_client.get(f"/workflows/{wf['id']}/runs/{run['id']}/status")
    assert status_resp.status_code == 200
    assert status_resp.json()["total_tasks"] == 2


def test_running_twice_produces_two_independent_runs(api_client):
    wf = _create_workflow(api_client)
    api_client.put(
        f"/workflows/{wf['id']}/definition",
        json={"tasks": [{"client_id": "a", "name": "solo", "task_type": "echo"}]},
    )

    run1 = api_client.post(f"/workflows/{wf['id']}/runs").json()
    run2 = api_client.post(f"/workflows/{wf['id']}/runs").json()
    assert run1["id"] != run2["id"]

    runs_list = api_client.get(f"/workflows/{wf['id']}/runs").json()
    assert {r["id"] for r in runs_list} == {run1["id"], run2["id"]}

    tasks1 = api_client.get(f"/workflows/{wf['id']}/runs/{run1['id']}/tasks").json()
    tasks2 = api_client.get(f"/workflows/{wf['id']}/runs/{run2['id']}/tasks").json()
    assert tasks1[0]["id"] != tasks2[0]["id"]


def test_run_snapshot_is_immutable_to_later_definition_edits(api_client):
    wf = _create_workflow(api_client)
    api_client.put(
        f"/workflows/{wf['id']}/definition",
        json={"tasks": [{"client_id": "a", "name": "original", "task_type": "echo"}]},
    )
    run = api_client.post(f"/workflows/{wf['id']}/runs").json()

    api_client.put(
        f"/workflows/{wf['id']}/definition",
        json={"tasks": [{"client_id": "x", "name": "changed", "task_type": "sleep"}]},
    )

    reloaded = api_client.get(f"/workflows/{wf['id']}/runs/{run['id']}").json()
    names = [t["name"] for t in reloaded["definition_snapshot"]["task_definitions"]]
    assert names == ["original"]


def test_run_ownership_enforced(api_app):
    token_owner = _register_and_login(api_app, "owner-run@example.com")
    token_other = _register_and_login(api_app, "other-run@example.com")

    wf = api_app.post(
        "/workflows", json={"name": "wf"}, headers=_auth(token_owner)
    ).json()
    api_app.put(
        f"/workflows/{wf['id']}/definition",
        json={"tasks": [{"client_id": "a", "name": "a", "task_type": "echo"}]},
        headers=_auth(token_owner),
    )
    run = api_app.post(f"/workflows/{wf['id']}/runs", headers=_auth(token_owner)).json()

    assert (
        api_app.get(f"/workflows/{wf['id']}/runs", headers=_auth(token_other)).status_code
        == 404
    )
    assert (
        api_app.get(
            f"/workflows/{wf['id']}/runs/{run['id']}", headers=_auth(token_other)
        ).status_code
        == 404
    )
    assert (
        api_app.post(f"/workflows/{wf['id']}/runs", headers=_auth(token_other)).status_code
        == 404
    )


def test_run_for_nonexistent_workflow_returns_404(api_client):
    assert api_client.post("/workflows/999999/runs").status_code == 404
    assert api_client.get("/workflows/999999/runs").status_code == 404
