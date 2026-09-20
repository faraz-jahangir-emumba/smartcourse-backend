"""Engine and session handling.

The engine manages a pool of connections. Sessions are borrowed from it, one
per request, and handed back when the request finishes.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from smartcourse.config import get_settings

_settings = get_settings()

# Creating an engine does not connect to anything. SQLAlchemy opens the first
# connection when something actually runs a query, so this is safe at import.
engine: AsyncEngine = create_async_engine(
    _settings.database_url,
    # Log every statement. Invaluable while learning - you see the SQL your
    # Python turned into. Turn it off once it becomes noise.
    echo=True,
    # How many connections to keep open and reuse. Opening one costs tens of
    # milliseconds, so reusing them matters more than it sounds.
    pool_size=10,
    # Extra connections allowed temporarily during a spike.
    max_overflow=5,
    # Check a pooled connection is still alive before handing it out. Without
    # this, restarting the Postgres container leaves stale connections in the
    # pool and the next few requests fail for no visible reason.
    pool_pre_ping=True,
)

# A factory, not a session. Call it to get a new one.
#
# expire_on_commit=False: by default SQLAlchemy forgets every loaded value
# after a commit, so reading course.title afterwards triggers another query -
# which, being async, would need an await you did not write. Switching it off
# keeps the objects usable after commit.
SessionFactory = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Provide a session for one request, then clean up.

    FastAPI uses this as a dependency: an endpoint asks for a session and this
    runs to supply one. The code after `yield` runs once the request is done.

    Commit on success, roll back on any exception. That is the guarantee that
    matters - a request that fails halfway leaves nothing behind. An enrollment
    is never recorded without its progress row.
    """
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close every pooled connection. Called when the app shuts down."""
    await engine.dispose()
