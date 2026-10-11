"""Account routes: register / login / logout / me.

Same thin-translation-only convention as ``api/routes.py`` -- every
mutation goes through ``auth/persistence.py`` (never raw SQL here), and
password/token handling goes through ``auth/passwords.py``/``auth/
tokens.py``. This module is the one place a plaintext password is ever
held in memory, and only for the duration of hashing/verifying it --
never logged, never stored.
"""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from psycopg_pool import ConnectionPool

from api.auth_dependency import AuthenticatedUser, get_current_user
from api.schemas import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from auth import persistence
from auth.passwords import hash_password, verify_password
from auth.tokens import create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])

# A fixed, valid bcrypt hash of an unrelated dummy string -- not any real
# user's password. Used only to burn a bcrypt verification's worth of
# time when the email lookup misses, below, so "unknown email" and
# "known email, wrong password" take statistically indistinguishable
# time -- otherwise the "same 401 either way" comment a few lines down
# would be defeated by a timing side-channel (short-circuiting `or`
# skips verify_password entirely when user is None, which is
# measurably faster than actually running bcrypt). Found during the
# Phase 8 security review.
_DUMMY_PASSWORD_HASH = "$2b$12$ZkojKfSLKMr8CjXe7QuaietLpSwHwBl7H3YrpTs3IIRCuuMuQAKZ2"


def get_pool(request: Request) -> ConnectionPool:
    return request.app.state.pool


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, pool: ConnectionPool = Depends(get_pool)) -> UserResponse:
    try:
        row = persistence.create_user(
            pool, email=body.email, password_hash=hash_password(body.password)
        )
    except psycopg.errors.UniqueViolation:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="email is already registered"
        )
    return UserResponse(**row)


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request, pool: ConnectionPool = Depends(get_pool)) -> TokenResponse:
    user = persistence.get_user_by_email(pool, email=body.email)
    # Deliberately the same 401 for "no such user" and "wrong password" --
    # distinguishing them would let a caller enumerate registered emails.
    # Also deliberately always calls verify_password exactly once on this
    # path, even when there's no real user to check against (using
    # _DUMMY_PASSWORD_HASH) -- so the response time doesn't leak which
    # case occurred either. See _DUMMY_PASSWORD_HASH's comment above.
    password_hash = user["password_hash"] if user is not None else _DUMMY_PASSWORD_HASH
    password_ok = verify_password(body.password, password_hash)
    if user is None or not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid email or password"
        )

    config = request.app.state.config
    token = create_access_token(
        user_id=user["id"],
        email=user["email"],
        secret=config.jwt_secret,
        expiry_minutes=config.jwt_expiry_minutes,
    )
    return TokenResponse(
        access_token=token,
        user=UserResponse(id=user["id"], email=user["email"], created_at=user["created_at"]),
    )


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout(current_user: AuthenticatedUser = Depends(get_current_user)) -> dict:
    # Stateless JWTs: there is no server-side session to invalidate. This
    # endpoint exists so the frontend has a single, honest place to call
    # before dropping its locally stored token -- it does not revoke the
    # token early (see auth/tokens.py's module docstring).
    return {"detail": "logged out"}


@router.get("/me", response_model=UserResponse)
def me(
    current_user: AuthenticatedUser = Depends(get_current_user),
    pool: ConnectionPool = Depends(get_pool),
) -> UserResponse:
    user = persistence.get_user_by_id(pool, user_id=current_user.id)
    if user is None:
        # The user existed when the token was issued but has since been
        # deleted -- there is no user-deletion endpoint today, so this is
        # unreachable in practice, but a stale/forged token must not be
        # trusted to fabricate a response.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found")
    return UserResponse(**user)
