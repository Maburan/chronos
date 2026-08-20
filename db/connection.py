"""PostgreSQL connection/session handling: psycopg 3 + psycopg_pool.

psycopg 3 (not psycopg2) is used because:

  - It's the actively maintained line, with a clean ``conn.transaction()``
    context manager that gives exactly the explicit, exception-safe
    transaction boundaries this persistence layer needs.
  - ``psycopg.rows.dict_row`` turns every query result into a plain
    ``{column_name: value}`` dict, which is exactly what
    ``db/models.py``'s row converters expect -- no ORM, no ``cursor
    .description`` bookkeeping.
  - ``psycopg_pool.ConnectionPool`` is the natural fit both for the future
    scheduler/worker fleet and for these tests, where genuinely separate,
    concurrent connections are required to exercise real Postgres row
    locking.

No ORM. The SQL in ``db/queries.py`` is intentionally hand-written and
Postgres-specific (``FOR UPDATE SKIP LOCKED``, partial indexes, a native
enum) -- an ORM would add abstraction with no corresponding benefit here.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def _configure_connection(conn: psycopg.Connection) -> None:
    """Applied to every connection the pool creates (and after resets).

    Two things matter here, both load-bearing for correctness elsewhere:

    1. ``row_factory = dict_row`` -- every query result becomes a
       ``{column_name: value}`` dict, which ``db/models.py`` relies on.
    2. ``SET TIME ZONE 'UTC'`` -- Postgres formats ``timestamptz`` output
       according to the *session's* time zone setting. Without pinning it
       explicitly, ``lease_expires_at``/``created_at``/``updated_at``
       could come back with a non-UTC (but still valid) fixed-offset
       ``tzinfo``, and ``core/transitions.py``'s ``_require_utc`` demands
       ``utcoffset() == timedelta(0)`` exactly -- so this is not
       cosmetic, it's what keeps every row read compatible with the
       frozen module's strict aware-UTC contract.

    The trailing ``commit()`` is required, not optional: ``configure`` runs
    with the connection NOT in autocommit, so ``SET TIME ZONE`` leaves the
    session in an open transaction (``INTRANS``). ``ConnectionPool._connect``
    checks that a freshly configured connection is back to ``IDLE`` and
    raises ``ProgrammingError`` (discarding the connection) otherwise --
    every connection the pool ever created would fail this check without
    the commit.
    """
    conn.row_factory = dict_row
    conn.execute("SET TIME ZONE 'UTC'")
    conn.commit()


def get_pool(dsn: str, **kwargs: Any) -> ConnectionPool:
    """Create (and open) a connection pool for ``dsn``.

    Extra keyword arguments (``min_size``, ``max_size``, etc.) are passed
    straight through to ``ConnectionPool`` -- callers such as the
    concurrency tests, which need many simultaneous real connections,
    should raise ``max_size`` accordingly.
    """
    return ConnectionPool(
        conninfo=dsn,
        configure=_configure_connection,
        open=True,
        **kwargs,
    )
