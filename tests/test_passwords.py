"""Unit tests for auth/passwords.py. No database required."""

from __future__ import annotations

import pytest

from auth.passwords import PasswordTooLongError, hash_password, verify_password


def test_hash_and_verify_roundtrip():
    hashed = hash_password("correct-horse-battery")
    assert verify_password("correct-horse-battery", hashed)


def test_verify_rejects_wrong_password():
    hashed = hash_password("correct-horse-battery")
    assert not verify_password("wrong-password", hashed)


def test_hash_never_returns_the_plaintext():
    hashed = hash_password("correct-horse-battery")
    assert hashed != "correct-horse-battery"
    assert hashed.startswith("$2")


def test_hash_password_rejects_passwords_over_72_bytes():
    with pytest.raises(PasswordTooLongError):
        hash_password("a" * 73)


def test_hash_password_accepts_exactly_72_bytes():
    hash_password("a" * 72)  # must not raise


def test_hash_password_rejects_multibyte_password_over_72_bytes():
    # Each "é" is 2 bytes in UTF-8, so 40 of them is 80 bytes -- over the
    # limit even though the character count (40) looks well under 72.
    with pytest.raises(PasswordTooLongError):
        hash_password("é" * 40)


def test_verify_password_returns_false_not_raise_for_malformed_hash():
    assert verify_password("anything", "not-a-real-bcrypt-hash") is False
