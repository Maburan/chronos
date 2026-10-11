"""Password hashing -- bcrypt, via the ``bcrypt`` package directly (not
``passlib``, which is effectively unmaintained). Plaintext passwords are
never stored or logged anywhere; only the bcrypt hash produced here ever
reaches ``users.password_hash``.
"""

from __future__ import annotations

import bcrypt

# bcrypt's own hard limit -- hashpw raises ValueError beyond this many
# UTF-8-encoded bytes. api/schemas.py::RegisterRequest already enforces a
# 72-*character* cap so this is normally unreachable from the API, but
# this check is kept here too as defense in depth for any other caller of
# hash_password that doesn't go through that schema.
_BCRYPT_MAX_PASSWORD_BYTES = 72


class PasswordTooLongError(ValueError):
    """Raised when a password's UTF-8 encoding exceeds bcrypt's 72-byte
    limit. A ValueError subclass so existing bare `except ValueError`
    handling (if any) still catches it, but distinctly named so a caller
    can choose to handle "too long" differently from other ValueErrors."""


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > _BCRYPT_MAX_PASSWORD_BYTES:
        raise PasswordTooLongError(
            f"password exceeds bcrypt's {_BCRYPT_MAX_PASSWORD_BYTES}-byte limit"
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time comparison via bcrypt's own ``checkpw`` -- never a
    plain ``==`` on hashes or plaintext. Returns ``False`` (never raises)
    for a malformed stored hash, so a corrupted row can't crash login."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False
