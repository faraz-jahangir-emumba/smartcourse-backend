"""The SmartCourse API.

Built by a factory rather than at import time, so tests can construct an
isolated app and the worker processes can import from this package without
starting a web server.

Run with: uv run uvicorn smartcourse.main:create_app --factory --reload
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from smartcourse.api.errors import register_exception_handlers
from smartcourse.api.routers import auth
from smartcourse.infra.db.session import dispose_engine

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown.

    Everything before `yield` runs once at startup, everything after once at
    shutdown. Disposing the engine closes the pooled connections rather than
    leaving Postgres holding them until it notices they are gone.
    """
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="SmartCourse API",
        description="Backend for EduCorp's course delivery platform.",
        version="0.1.0",
        lifespan=lifespan,
    )

    register_exception_handlers(app)

    # Built here rather than at module level, so nothing is shared between
    # apps. Tests create their own instance, and a router mutated at import
    # time would carry state between them.
    #
    # Everything business-facing hangs off /api/v1. Health sits outside it: it
    # is an operational surface, not part of the versioned contract, and a
    # monitoring check should not break because the API version moved on.
    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(auth.router)
    app.include_router(api_v1)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        """The process is up. Says nothing about the database.

        A readiness check that also tests Postgres and Redis arrives in
        Module 3 - a service can be running while unable to do any work, and
        the two need different answers.
        """
        return {"status": "ok"}

    return app
