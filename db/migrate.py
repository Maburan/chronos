"""Minimal, hand-rolled migration runner.

Deliberately not Alembic or any other migration framework -- the schema
is a handful of `.sql` files applied in filename order, tracked in a
single `schema_migrations` table. That's the whole feature set this
project needs right now.
"""

from __future__ import annotations

import pathlib

import psycopg

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"

# schema_migrations is bootstrapped here, by the runner itself, rather than
# by 0001_init.sql. The runner needs this table to exist *before* it can
# even check which versioned migrations have been applied, so a versioned
# migration file can't be the thing that creates its own prerequisite.
_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def discover_migrations(
    migrations_dir: pathlib.Path = MIGRATIONS_DIR,
) -> list[tuple[str, pathlib.Path]]:
    """Every ``*.sql`` file in ``migrations_dir``, sorted by filename, paired
    with its version identifier (filename without the ``.sql`` suffix, e.g.
    ``0001_init``)."""
    files = sorted(migrations_dir.glob("*.sql"))
    return [(f.stem, f) for f in files]


def applied_versions(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def apply_migrations(
    conn: psycopg.Connection, migrations_dir: pathlib.Path = MIGRATIONS_DIR
) -> list[str]:
    """Apply every not-yet-applied ``*.sql`` migration in filename order.

    Each migration's SQL and its ``schema_migrations`` bookkeeping row are
    committed together, atomically, in a single transaction -- a crash
    mid-file can never leave a half-applied, unrecorded migration behind.
    Re-running against an already-up-to-date database is a no-op and
    returns an empty list.
    """
    with conn.cursor() as cur:
        cur.execute(_BOOTSTRAP_SQL)
    conn.commit()

    already_applied = applied_versions(conn)
    newly_applied: list[str] = []

    for version, path in discover_migrations(migrations_dir):
        if version in already_applied:
            continue
        sql = path.read_text(encoding="utf-8")
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (version,),
                )
        newly_applied.append(version)

    return newly_applied
