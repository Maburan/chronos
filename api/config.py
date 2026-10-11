"""API configuration, sourced from environment variables -- same
``from_env()`` convention as every other config class in this project.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """Raised when required API configuration is missing."""


@dataclass(frozen=True)
class ApiConfig:
    """``database_url`` (``CHRONOS_DATABASE_URL``) is the API's own
    runtime PostgreSQL connection string -- distinct from every other
    DSN-like variable in this project (``CHRONOS_TEST_DATABASE_URL``,
    ``CHRONOS_RABBITMQ_URL``, ``CHRONOS_REDIS_URL``), which are either
    test-only or belong to a different dependency.
    """

    database_url: str
    host: str = "127.0.0.1"
    port: int = 8000
    pool_min_size: int = 1
    pool_max_size: int = 10
    # Origins allowed to call this API from a browser (CORS). Defaults to
    # the Vite dev server's default origin only -- this exists solely so
    # the local frontend (api/../frontend, a separate dev server on a
    # different port) can call this API at all; it grants no additional
    # server-side capability and is not read by anything else in this
    # project. Comma-separated to override for a different dev port.
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")
    # JWT signing secret. No default in from_env() -- a missing secret
    # must fail loudly at startup, never silently fall back to a
    # guessable value. Tests construct ApiConfig directly with a fixed
    # test secret (see tests/conftest.py), same pattern as database_url.
    jwt_secret: str = ""
    jwt_expiry_minutes: int = 60 * 24

    @classmethod
    def from_env(cls) -> "ApiConfig":
        database_url = os.environ.get("CHRONOS_DATABASE_URL")
        if not database_url:
            raise ConfigError(
                "CHRONOS_DATABASE_URL is not set; the API needs a "
                "PostgreSQL connection string, never hard-coded"
            )
        jwt_secret = os.environ.get("CHRONOS_JWT_SECRET")
        if not jwt_secret:
            raise ConfigError(
                "CHRONOS_JWT_SECRET is not set; the API needs a secret to "
                "sign/verify auth tokens, never hard-coded or defaulted"
            )
        cors_origins_env = os.environ.get("CHRONOS_API_CORS_ORIGINS")
        cors_origins = (
            tuple(o.strip() for o in cors_origins_env.split(",") if o.strip())
            if cors_origins_env
            else cls.cors_origins
        )
        return cls(
            database_url=database_url,
            host=os.environ.get("CHRONOS_API_HOST", "127.0.0.1"),
            port=int(os.environ.get("CHRONOS_API_PORT", "8000")),
            pool_min_size=int(os.environ.get("CHRONOS_API_POOL_MIN_SIZE", "1")),
            pool_max_size=int(os.environ.get("CHRONOS_API_POOL_MAX_SIZE", "10")),
            cors_origins=cors_origins,
            jwt_secret=jwt_secret,
            jwt_expiry_minutes=int(
                os.environ.get("CHRONOS_JWT_EXPIRY_MINUTES", str(60 * 24))
            ),
        )
