"""Shared test fixtures.

Isolation happens at two levels, and both are needed.

**A throwaway database per run.** `smartcourse_test` is created before the
first test, migrated from empty, and dropped afterwards. That keeps the suite
away from your development data entirely - the rollback below *should* prevent
any leakage, but "should" is doing a lot of work, and one fixture bug would
put test rows in the database you are working in.

It also means every run applies every migration to an empty database, which is
the only thing that actually checks they work from scratch. Applying them once
by hand in September proves nothing about a fresh clone.

**A transaction per test.** Each test runs inside one and it is rolled back
afterwards, so tests cannot see each other's data. Without this the tests
would share the run's database and order would start to matter.

Needs Postgres running:

    docker compose up -d
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

# Set before importing anything from smartcourse. Settings are read on first
# use and then cached, and infra.db.session reads them at import time - so by
# the time the imports below have run, the database name is already fixed.
# Putting this line after them would silently point the tests at the
# development database.
os.environ["POSTGRES_DB"] = "smartcourse_test"

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

from smartcourse.config import get_settings  # noqa: E402
from smartcourse.infra.db.session import get_session  # noqa: E402
from smartcourse.main import create_app  # noqa: E402

PASSWORD = "correct-horse-battery"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_DB = "smartcourse_test"


async def _run_on_maintenance_db(*statements: str) -> None:
    """Run statements against the `postgres` database.

    Creating or dropping a database cannot be done from inside that database,
    so this connects to the always-present `postgres` one instead. asyncpg
    directly rather than SQLAlchemy, because CREATE DATABASE cannot run inside
    a transaction and SQLAlchemy opens one by default.
    """
    import asyncpg

    settings = get_settings()
    connection = await asyncpg.connect(
        user=settings.postgres_user,
        password=settings.postgres_password,
        host=settings.postgres_host,
        port=settings.postgres_port,
        database="postgres",
    )
    try:
        for statement in statements:
            await connection.execute(statement)
    finally:
        await connection.close()


@pytest.fixture(scope="session", autouse=True)
def test_database() -> Iterator[None]:
    """Create the test database, migrate it, and drop it at the end.

    Deliberately a *synchronous* fixture. A session-scoped async one would run
    in a different event loop from the function-scoped tests, which is the
    same clash that forced a per-test engine below. Wrapping each async call
    in its own asyncio.run sidesteps it: the loop opens and closes here and
    never meets the tests'.

    autouse, so no test has to remember to ask for it.

    WITH (FORCE) on the drop disconnects anything still attached. Without it a
    single leaked connection - from a crashed test, or a debugger session -
    makes the drop hang until it is closed by hand.
    """
    asyncio.run(
        _run_on_maintenance_db(
            f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)',
            f'CREATE DATABASE "{TEST_DB}"',
        )
    )

    # Migrations, run from empty. This is the part that proves they work on a
    # fresh clone - alembic's env.py reads the same Settings, which now point
    # at the test database because of the environment variable above.
    from alembic import command
    from alembic.config import Config

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(config, "head")

    yield

    asyncio.run(
        _run_on_maintenance_db(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
    )


@pytest.fixture
async def engine(test_database: None) -> AsyncIterator:
    """A fresh engine per test.

    Sharing one across the whole run would be faster, and does not work:
    pytest-asyncio gives each test its own event loop, while an asyncpg
    connection belongs to the loop that opened it. Reusing it across loops
    fails with "got Future attached to a different loop".

    NullPool because pooling is pointless here - each engine is used by
    exactly one test and then disposed. It also guarantees no connection
    outlives the loop it was created in.
    """
    eng = create_async_engine(
        get_settings().database_url, echo=False, poolclass=NullPool
    )
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    """A session whose work is always thrown away.

    The mechanism: open a connection, start a transaction on it, and bind the
    session to that connection. Anything the test does - including the
    session.commit() our request handler performs - happens *inside* the outer
    transaction. Rolling it back at the end discards all of it.

    That is what makes each test independent without truncating tables between
    them.
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        maker = async_sessionmaker(
            bind=connection, class_=AsyncSession, expire_on_commit=False
        )
        async with maker() as s:
            yield s
        await transaction.rollback()


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """An HTTP client wired to the app, sharing the test's transaction.

    dependency_overrides is the point. The app would normally build its own
    session, which would sit outside our transaction and commit for real.
    Overriding get_session hands every endpoint the same rolled-back session
    the test is using, so a request's writes are visible to the test and
    survive no longer than it does.

    ASGITransport means requests go straight into the app in-process. No
    server, no port, no network - which makes the suite fast and removes a
    whole category of flakiness.
    """
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as c:
        yield c

    app.dependency_overrides.clear()


# --- helpers ---------------------------------------------------------------
#
# Registration and login happen in nearly every test. Doing it through the API
# rather than by inserting rows means the tests exercise the real path,
# including password hashing - and a test user is created the same way a real
# one is.


async def register(
    client: AsyncClient, email: str, roles: list[str] | None = None
) -> dict:
    response = await client.post(
        "/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "full_name": email.split("@")[0].title(),
            "roles": roles or ["student"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def auth_header(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(
        "/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
