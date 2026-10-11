"""Demo/dev tool: creates a few realistic example workflows through the
real Chronos HTTP API (never direct database writes) so the frontend has
something more interesting to show than a hand-built single task.

    python -m scripts.seed_demo_workflows --api-url http://localhost:8000

Requires the API, a worker (registered with the sleep/echo/
python_expression task types -- the default set `python -m worker`
already registers, see worker/tasks/__init__.py), the outbox relay, and
RabbitMQ all running for the created tasks to actually execute -- this
script only ever calls the public API, exactly like the frontend does. It
registers a demo user if one doesn't already exist (email/password
overridable via --email/--password), then creates:

  1. A linear pipeline (fetch -> transform -> report) -- demonstrates
     plain sequential dependencies.
  2. A diamond/parallel workflow (A -> {B, C} -> D) -- demonstrates
     fan-out/fan-in and real parallelism.
  3. A retry-demo workflow (one echo task with fail_until_attempt) --
     demonstrates the retry/attempt_count state-transition path.

Not a production script, not imported by any application code -- a
manual demo aid only.
"""

from __future__ import annotations

import argparse
import sys

import httpx


def _register_and_login(client: httpx.Client, *, email: str, password: str) -> str:
    register = client.post("/auth/register", json={"email": email, "password": password})
    if register.status_code not in (201, 409):
        register.raise_for_status()
    login = client.post("/auth/login", json={"email": email, "password": password})
    login.raise_for_status()
    return login.json()["access_token"]


def _create_workflow(client: httpx.Client, name: str) -> int:
    resp = client.post("/workflows", json={"name": name})
    resp.raise_for_status()
    return resp.json()["id"]


def _create_task(
    client: httpx.Client,
    workflow_id: int,
    *,
    name: str,
    task_type: str,
    payload: dict | None = None,
    max_attempts: int = 1,
    depends_on_ids: list[int] | None = None,
) -> int:
    resp = client.post(
        f"/workflows/{workflow_id}/tasks",
        json={
            "name": name,
            "task_type": task_type,
            "max_attempts": max_attempts,
            "payload": payload,
            "depends_on_ids": depends_on_ids,
        },
    )
    resp.raise_for_status()
    return resp.json()["id"]


def seed_linear_pipeline(client: httpx.Client) -> int:
    """Fetch -> Transform -> Report. Each stage is a safe local operation
    (sleep stands in for "fetch," python_expression for "transform," echo
    for the final report) so this runs without any external network
    dependency."""
    workflow_id = _create_workflow(client, "Data processing pipeline")

    fetch_id = _create_task(
        client, workflow_id, name="Fetch data", task_type="sleep", payload={"seconds": 1}
    )
    transform_id = _create_task(
        client,
        workflow_id,
        name="Transform data",
        task_type="python_expression",
        payload={"expression": "(records * 2) - dropped", "variables": {"records": 100, "dropped": 3}},
        depends_on_ids=[fetch_id],
    )
    _create_task(
        client,
        workflow_id,
        name="Generate report",
        task_type="echo",
        payload={"message": "pipeline complete"},
        depends_on_ids=[transform_id],
    )
    return workflow_id


def seed_parallel_diamond(client: httpx.Client) -> int:
    """A -> {B, C} -> D: demonstrates fan-out (B and C both become READY
    as soon as A succeeds, and can run concurrently on different workers)
    and fan-in (D waits for both)."""
    workflow_id = _create_workflow(client, "Parallel diamond workflow")

    a_id = _create_task(client, workflow_id, name="Task A", task_type="sleep", payload={"seconds": 1})
    b_id = _create_task(
        client, workflow_id, name="Task B", task_type="sleep", payload={"seconds": 2}, depends_on_ids=[a_id]
    )
    c_id = _create_task(
        client, workflow_id, name="Task C", task_type="sleep", payload={"seconds": 1}, depends_on_ids=[a_id]
    )
    _create_task(
        client,
        workflow_id,
        name="Task D",
        task_type="echo",
        payload={"message": "fan-in complete"},
        depends_on_ids=[b_id, c_id],
    )
    return workflow_id


def seed_retry_demo(client: httpx.Client) -> int:
    """One echo task that fails on its first two attempts and succeeds on
    the third -- watch attempt_count climb 1 -> 2 -> 3 in the UI."""
    workflow_id = _create_workflow(client, "Retry demo")
    _create_task(
        client,
        workflow_id,
        name="Flaky task",
        task_type="echo",
        payload={"message": "retry demo", "fail_until_attempt": 3},
        max_attempts=5,
    )
    return workflow_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--email", default="demo@chronos.local")
    parser.add_argument("--password", default="chronos-demo-password")
    args = parser.parse_args()

    with httpx.Client(base_url=args.api_url, timeout=10.0) as client:
        token = _register_and_login(client, email=args.email, password=args.password)
        client.headers["Authorization"] = f"Bearer {token}"

        linear_id = seed_linear_pipeline(client)
        diamond_id = seed_parallel_diamond(client)
        retry_id = seed_retry_demo(client)

    print(f"Logged in as {args.email}")
    print(f"Created workflow #{linear_id}: Data processing pipeline")
    print(f"Created workflow #{diamond_id}: Parallel diamond workflow")
    print(f"Created workflow #{retry_id}: Retry demo")
    print("Open the frontend and watch these execute as workers pick them up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
