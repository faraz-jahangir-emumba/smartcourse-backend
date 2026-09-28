"""Alembic environment.

Two changes from the generated template:

1. The database URL comes from our Settings, not from alembic.ini, so there is
   one place credentials are defined and none of them sit in a tracked file.
2. target_metadata points at our Base, so `alembic revision --autogenerate` can
   compare the models against the real database.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from smartcourse.config import get_settings

# Importing the models package registers every table on Base.metadata.
# Alembic reads that registry to work out what the schema should look like, so
# a model missing from here is a table that never gets a migration - silently.
from smartcourse.infra.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Supply the URL at runtime rather than leaving it in alembic.ini.
# escape_percent because configparser treats % specially, and a password could
# contain one.
config.set_main_option(
    "sqlalchemy.url", get_settings().database_url.replace("%", "%%")
)

target_metadata = Base.metadata


def _configure(**kwargs: object) -> None:
    """Options shared by both modes.

    compare_type and compare_server_default make autogenerate notice more than
    just added and removed tables - changing a column from String(200) to
    String(300), or altering a default, is detected instead of silently
    ignored.
    """
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        **kwargs,  # type: ignore[arg-type]
    )


def run_migrations_offline() -> None:
    """Generate SQL without connecting to anything.

    `alembic upgrade head --sql` prints the statements instead of running them,
    which is how a migration gets handed to a DBA in places that do not let an
    application alter its own schema.
    """
    _configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # NullPool: a migration is one short-lived task. Pooling connections
        # for it would leave them open after the command finishes.
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
