"""Phase 10: /healthz and /readyz tests.

Real PostgreSQL for the positive cases (via the ``api_client`` fixture).
For the negative /readyz case, ``persistence.check_connection`` is
monkeypatched to raise -- a real, genuinely unreachable database takes
~30s to time out against ``psycopg_pool``'s default pool timeout (no
configurable override exists on ``ApiConfig`` today), which is too slow
for this suite; that exact "does check_connection detect an unreachable
database" behavior is already covered, fast and for real, in
tests/test_persistence.py::test_check_connection_raises_when_unreachable.
This test's job is narrower: prove the /readyz route correctly
translates a check_connection failure into a 503, nothing more.
"""

from __future__ import annotations

from db import persistence


def test_healthz_returns_ok(api_client):
    response = api_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_does_not_depend_on_the_database(api_client, monkeypatch):
    """/healthz's handler takes no pool dependency at all -- confirmed by
    inspection (api/health.py::healthz has no Depends(get_pool)) and
    reinforced here: even if check_connection would fail, healthz must
    never call it."""

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("healthz must never call check_connection")

    monkeypatch.setattr(persistence, "check_connection", _fail_if_called)

    response = api_client.get("/healthz")
    assert response.status_code == 200


def test_readyz_returns_ok_when_database_reachable(api_client):
    response = api_client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_returns_503_when_database_unreachable(api_client, monkeypatch):
    def _raise(pool):
        raise RuntimeError("simulated: database unreachable")

    monkeypatch.setattr(persistence, "check_connection", _raise)

    response = api_client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert "simulated: database unreachable" in body["detail"]
