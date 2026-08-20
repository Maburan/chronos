"""Schema-bootstrap entrypoint.

    python -m db

Reads ``CHRONOS_DATABASE_URL``, connects, and runs the existing,
unmodified ``db.migrate.apply_migrations`` -- the exact same migration
mechanism every test fixture already uses (see
``tests/conftest.py::pg_pool``). This is not a second migration system;
it is the smallest possible wiring to run the one that already exists
outside of a test fixture, for a real, freshly-created database that
nothing else in this project currently has any way to bootstrap.
"""

from __future__ import annotations

import os
import sys

import psycopg

from db.migrate import apply_migrations


def main() -> int:
    database_url = os.environ.get("CHRONOS_DATABASE_URL")
    if not database_url:
        print("CHRONOS_DATABASE_URL is not set", file=sys.stderr)
        return 1

    with psycopg.connect(database_url) as conn:
        applied = apply_migrations(conn)

    if applied:
        print(f"applied: {', '.join(applied)}")
    else:
        print("already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
