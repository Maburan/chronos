"""SQL for the ``users`` table, kept separate from ``db/queries.py``
because user accounts are an authentication concern layered on top of
Chronos's orchestration schema, not part of the orchestration engine
itself. Same "every statement is a named constant" convention as
``db/queries.py``.
"""

INSERT_USER = """
INSERT INTO users (email, password_hash)
VALUES (%(email)s, %(password_hash)s)
RETURNING id, email, created_at;
"""

SELECT_USER_BY_EMAIL = """
SELECT id, email, password_hash, created_at
FROM users
WHERE email = %(email)s;
"""

SELECT_USER_BY_ID = """
SELECT id, email, created_at
FROM users
WHERE id = %(user_id)s;
"""
