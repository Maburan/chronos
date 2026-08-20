"""The API application factory.

Owns exactly one thing beyond routing: the connection pool's lifecycle
(created on startup, closed on shutdown, via FastAPI's lifespan context
manager). Everything else -- migrations, schema, state legality -- is
entirely the existing persistence-layer responsibility; this module does
not run migrations itself (matching every other entrypoint in this
project, which assumes the schema is already current -- see
db/migrate.py).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.config import ApiConfig
from api.health import health_router
from api.routes import router
from db.connection import get_pool


def create_app(config: Optional[ApiConfig] = None) -> FastAPI:
    """Build the FastAPI app. ``config`` can be supplied directly (tests
    do this with a test database URL); defaults to
    ``ApiConfig.from_env()`` for real runtime use."""
    resolved_config = config or ApiConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = get_pool(
            resolved_config.database_url,
            min_size=resolved_config.pool_min_size,
            max_size=resolved_config.pool_max_size,
        )
        app.state.pool = pool
        try:
            yield
        finally:
            pool.close()

    app = FastAPI(title="Chronos API", lifespan=lifespan)
    # Dev-only CORS for the separately-served frontend (see api/config.py's
    # cors_origins docstring). Adds no server-side capability beyond
    # letting a browser at one of these origins read responses; every
    # route's own logic, auth (none), and persistence behavior is
    # unchanged.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved_config.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    app.include_router(health_router)
    return app
