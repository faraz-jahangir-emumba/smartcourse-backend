"""Creating the first admin.

The seed opens its own session through SessionFactory and commits for real -
there is no request to hang off. So these tests cannot use the `client`
fixture, and they clean up after themselves.

get_settings is cached, so each test clears it after changing the
environment. Without that the first test's values would be handed to every
one after it.
"""

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smartcourse.config import get_settings
from smartcourse.infra.db.models.user import User
from smartcourse.infra.db.session import dispose_engine
from smartcourse.seed import seed_admin

EMAIL = "seeded-admin@example.com"
PASSWORD = "a-long-enough-password"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _dispose_global_engine():
    """Close the seed's connections before the test's event loop does.

    seed_admin uses the module-level SessionFactory, not the per-test engine
    the rest of the suite uses - it has to, because that is what it uses in
    production. But pytest-asyncio gives each test its own loop, and an
    asyncpg connection belongs to the loop that opened it. Left open, the
    second test to run the seed inherits a connection whose loop is gone and
    fails deep inside asyncio with "NoneType has no attribute send".

    Same root cause as the per-test engine in conftest, from the other side.
    """
    yield
    await dispose_engine()


def _configure(monkeypatch, *, email: str | None, password: str | None) -> None:
    """Set the environment, then clear the settings cache.

    Two traps here, both silent.

    **Unset means empty, not deleted.** Settings reads the environment first
    and then .env, so deleting a variable only falls through to whatever
    .env says. A developer with ADMIN_EMAIL in their own .env would see
    these tests pass or fail depending on their machine - which is exactly
    how the first version of this file behaved, until one did.
    Setting the variable to "" overrides .env, and the seed treats empty and
    absent the same way.

    **Clear the cache after, not before.** get_settings is cached, and the
    `engine` fixture calls it during setup - so by the time the test body
    runs, a Settings built before monkeypatch touched anything is already
    cached.
    """
    for name, value in (("ADMIN_EMAIL", email), ("ADMIN_PASSWORD", password)):
        monkeypatch.setenv(name, value if value is not None else "")
    get_settings.cache_clear()


async def _admins(engine, email: str = EMAIL) -> list[User]:
    maker = async_sessionmaker(bind=engine, class_=AsyncSession)
    async with maker() as session:
        rows = await session.scalars(select(User).where(User.email == email))
        return list(rows)


async def _cleanup(engine, email: str = EMAIL) -> None:
    maker = async_sessionmaker(bind=engine, class_=AsyncSession)
    async with maker() as session:
        await session.execute(delete(User).where(User.email == email))
        await session.commit()


async def test_nothing_configured_skips_and_succeeds(engine, monkeypatch) -> None:
    """The case that makes the compose service safe.

    This runs on every `docker compose up`, and a fresh clone has no .env.
    Failing there would mean the stack cannot start until somebody configures
    an account they may not want.
    """
    _configure(monkeypatch, email=None, password=None)

    assert await seed_admin() == 0


async def test_half_configured_fails(engine, monkeypatch) -> None:
    """Not a choice - a typo. ADMIN_EMIAL, or a variable that never reached
    the container. Skipping quietly is how somebody sets a password, sees no
    error, and cannot work out why they cannot log in."""
    _configure(monkeypatch, email=None, password=PASSWORD)

    assert await seed_admin() == 1


async def test_a_short_password_is_refused(engine, monkeypatch) -> None:
    """An admin password should not be weaker than a student's."""
    _configure(monkeypatch, email=EMAIL, password="short")

    assert await seed_admin() == 1
    assert await _admins(engine) == []


async def test_it_creates_an_admin_and_running_twice_changes_nothing(
    engine, monkeypatch
) -> None:
    """Idempotent, which is what makes it safe to run on every startup.

    Also checks the email is normalised the same way registration does it -
    otherwise Admin@x.com and admin@x.com are two accounts, and the second
    run would create a duplicate instead of finding the first.
    """
    _configure(monkeypatch, email=EMAIL.upper(), password=PASSWORD)

    try:
        assert await seed_admin() == 0

        created = await _admins(engine)
        assert len(created) == 1
        assert created[0].roles == ["admin"]
        assert created[0].email == EMAIL
        # Stored as a hash, like any other password - the seed does not get
        # to take a shortcut just because it runs outside a request.
        assert created[0].password.startswith("$argon2")
        assert created[0].password != PASSWORD

        get_settings.cache_clear()
        assert await seed_admin() == 0
        assert len(await _admins(engine)) == 1
    finally:
        await _cleanup(engine)


async def test_an_existing_account_is_left_alone(engine, monkeypatch) -> None:
    """Silently promoting an existing student because somebody reused an
    address is surprising, and surprising is the wrong quality in the one
    command that creates privileged accounts."""
    maker = async_sessionmaker(bind=engine, class_=AsyncSession)
    async with maker() as session:
        session.add(
            User(
                email=EMAIL,
                password="x",
                full_name="Already Here",
                roles=["student"],
            )
        )
        await session.commit()

    _configure(monkeypatch, email=EMAIL, password=PASSWORD)

    try:
        assert await seed_admin() == 0

        after = await _admins(engine)
        assert len(after) == 1
        assert after[0].roles == ["student"]
    finally:
        await _cleanup(engine)
