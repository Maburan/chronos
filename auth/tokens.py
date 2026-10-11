"""Stateless JWT issuance/verification for the API's bearer-token auth.

No session table, no server-side revocation list -- a token is valid
until it expires, full stop. This is a deliberate scope decision (see
README/HANDOVER): acceptable for a project-scale multi-user demo, not a
claim that logout actually invalidates a token early.
"""

from __future__ import annotations

import time
from typing import TypedDict

import jwt

_ALGORITHM = "HS256"


class TokenError(Exception):
    """Raised for any invalid, expired, or malformed token. The caller
    (``api/auth_dependency.py``) treats this uniformly as 401 -- it never
    needs to distinguish "expired" from "tampered" from "malformed"."""


class TokenPayload(TypedDict):
    user_id: int
    email: str


def create_access_token(
    *, user_id: int, email: str, secret: str, expiry_minutes: int
) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "email": email,
        "iat": now,
        "exp": now + expiry_minutes * 60,
    }
    return jwt.encode(payload, secret, algorithm=_ALGORITHM)


def decode_access_token(token: str, *, secret: str) -> TokenPayload:
    try:
        payload = jwt.decode(token, secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc

    try:
        user_id = int(payload["sub"])
        email = payload["email"]
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenError("token payload missing required claims") from exc

    return TokenPayload(user_id=user_id, email=email)
