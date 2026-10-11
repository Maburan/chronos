"""Auth tests: register / login / logout / me, against the real API and
real PostgreSQL (``api_app`` -- the unauthenticated app fixture, since
every test here is about establishing identity itself, not something that
should start pre-authenticated like ``api_client``)."""

from __future__ import annotations


def _register(api_app, email="alice@example.com", password="correct-horse-battery"):
    return api_app.post("/auth/register", json={"email": email, "password": password})


def _login(api_app, email="alice@example.com", password="correct-horse-battery"):
    return api_app.post("/auth/login", json={"email": email, "password": password})


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def test_register_returns_201_and_user_shape(api_app):
    response = _register(api_app)

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "alice@example.com"
    assert isinstance(body["id"], int)
    assert "created_at" in body
    assert "password" not in body
    assert "password_hash" not in body


def test_register_duplicate_email_returns_409(api_app):
    _register(api_app)
    response = _register(api_app)
    assert response.status_code == 409


def test_register_short_password_returns_422(api_app):
    response = api_app.post(
        "/auth/register", json={"email": "bob@example.com", "password": "short"}
    )
    assert response.status_code == 422


def test_register_password_over_72_chars_returns_422_not_500(api_app):
    """Regression test (found during the Phase 8 security review): bcrypt
    hard-rejects passwords whose UTF-8 encoding exceeds 72 bytes. Before
    api/schemas.py::RegisterRequest capped max_length at 72, a
    73+ character password reached bcrypt.hashpw and raised an unhandled
    ValueError, surfacing as a raw 500 instead of a clean validation
    error."""
    response = api_app.post(
        "/auth/register", json={"email": "toolong@example.com", "password": "a" * 100}
    )
    assert response.status_code == 422


def test_register_invalid_email_returns_422(api_app):
    response = api_app.post(
        "/auth/register", json={"email": "not-an-email", "password": "longenoughpassword"}
    )
    assert response.status_code == 422


def test_register_password_is_never_stored_in_plaintext(api_app, db):
    _register(api_app)
    with db.connection() as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE email = %s", ("alice@example.com",)
        ).fetchone()
    assert row["password_hash"] != "correct-horse-battery"
    # bcrypt hashes are always prefixed this way.
    assert row["password_hash"].startswith("$2")


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def test_login_returns_token_and_user(api_app):
    _register(api_app)

    response = _login(api_app)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert isinstance(body["access_token"], str) and body["access_token"]
    assert body["user"]["email"] == "alice@example.com"


def test_login_wrong_password_returns_401(api_app):
    _register(api_app)
    response = _login(api_app, password="wrong-password-entirely")
    assert response.status_code == 401


def test_login_unknown_email_returns_401(api_app):
    response = _login(api_app, email="nobody@example.com")
    assert response.status_code == 401


def test_login_wrong_password_and_unknown_email_return_identical_error_shape(api_app):
    """Deliberately not distinguishable -- see api/auth_routes.py::login's
    comment on why enumeration must not be possible via the error body."""
    _register(api_app)
    wrong_password = _login(api_app, password="wrong-password-entirely")
    unknown_email = _login(api_app, email="nobody@example.com")

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json()["detail"] == unknown_email.json()["detail"]


# ---------------------------------------------------------------------------
# token-protected routes: /auth/me, /auth/logout
# ---------------------------------------------------------------------------


def test_me_requires_a_token(api_app):
    response = api_app.get("/auth/me")
    assert response.status_code == 401


def test_me_returns_the_authenticated_user(api_app):
    _register(api_app)
    token = _login(api_app).json()["access_token"]

    response = api_app.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["email"] == "alice@example.com"


def test_me_rejects_malformed_authorization_header(api_app):
    response = api_app.get("/auth/me", headers={"Authorization": "not-a-bearer-token"})
    assert response.status_code == 401


def test_me_rejects_tampered_token(api_app):
    _register(api_app)
    token = _login(api_app).json()["access_token"]
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    response = api_app.get("/auth/me", headers={"Authorization": f"Bearer {tampered}"})

    assert response.status_code == 401


def test_me_rejects_token_signed_with_a_different_secret(api_app):
    from auth.tokens import create_access_token

    forged = create_access_token(
        user_id=1, email="alice@example.com", secret="a-different-secret", expiry_minutes=60
    )

    response = api_app.get("/auth/me", headers={"Authorization": f"Bearer {forged}"})

    assert response.status_code == 401


def test_logout_requires_a_token_and_returns_200(api_app):
    _register(api_app)
    token = _login(api_app).json()["access_token"]

    response = api_app.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200


def test_logout_without_a_token_returns_401(api_app):
    response = api_app.post("/auth/logout")
    assert response.status_code == 401
