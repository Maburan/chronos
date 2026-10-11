"""The auth dependency every protected route uses: read the
``Authorization: Bearer <token>`` header, verify it, load the calling
user. A FastAPI ``Depends`` (a plain function, not a class) so it can be
swapped out in tests exactly the same way ``api/routes.py::get_pool``
already is (see that module's docstring for the established pattern).
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from auth.tokens import TokenError, decode_access_token


@dataclass(frozen=True)
class AuthenticatedUser:
    id: int
    email: str


def get_current_user(request: Request) -> AuthenticatedUser:
    header = request.headers.get("authorization")
    if not header or not header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = header.split(" ", 1)[1].strip()

    config = request.app.state.config
    try:
        payload = decode_access_token(token, secret=config.jwt_secret)
    except TokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthenticatedUser(id=payload["user_id"], email=payload["email"])
