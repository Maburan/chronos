"""User account persistence. Mirrors ``db/persistence.py``'s style (one
function per operation, no SQL embedded outside ``auth/queries.py``), but
lives in its own module since accounts are an auth-layer concern, not
part of the orchestration engine ``db/persistence.py`` owns.

Note on error translation: like ``db.persistence.create_task`` (see
``api/routes.py``'s handling of ``ForeignKeyViolation``), this module
does not itself translate Postgres errors -- a duplicate email surfaces
as a plain ``psycopg.errors.UniqueViolation`` for the caller (``api/
auth_routes.py``) to catch and turn into a 409, matching the existing
project convention of translating DB errors at the route layer.
"""

from __future__ import annotations

from typing import Optional

from psycopg_pool import ConnectionPool

from auth import queries


def create_user(pool: ConnectionPool, *, email: str, password_hash: str) -> dict:
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    queries.INSERT_USER, {"email": email, "password_hash": password_hash}
                )
                row = cur.fetchone()
    return dict(row)


def get_user_by_email(pool: ConnectionPool, *, email: str) -> Optional[dict]:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(queries.SELECT_USER_BY_EMAIL, {"email": email})
            row = cur.fetchone()
    return dict(row) if row is not None else None


def get_user_by_id(pool: ConnectionPool, *, user_id: int) -> Optional[dict]:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(queries.SELECT_USER_BY_ID, {"user_id": user_id})
            row = cur.fetchone()
    return dict(row) if row is not None else None
