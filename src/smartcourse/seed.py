"""Create the first admin.

Admin is not self-assignable - `register_user` refuses it, deliberately,
because a public endpoint where the caller picks their own role would let
anyone become one. That leaves a gap: in a real deployment there was no way
to create an admin at all, so the three admin branches in services/course.py
were unreachable outside tests that build users directly. Raised in the Week 1
review.

This is the one place allowed to grant it, and it is not an endpoint.

    uv run python -m smartcourse.seed                               locally
    docker compose run --rm api uv run python -m smartcourse.seed   by hand

It also runs on its own as part of `docker compose up`, as a one-shot service
beside `migrate`.

Credentials come from ADMIN_EMAIL and ADMIN_PASSWORD. Neither has a default:
a default admin password is a published admin password, and this one would be
in the repository.
"""

import asyncio
import sys

from smartcourse.config import get_settings
from smartcourse.infra.db.models.user import ROLE_ADMIN, User
from smartcourse.infra.db.repositories import UserRepository
from smartcourse.infra.db.session import SessionFactory, dispose_engine
from smartcourse.infra.security import hash_password

# The same floor registration applies. An admin password should not be weaker
# than a student's.
MIN_PASSWORD_LENGTH = 8


async def seed_admin() -> int:
    """Create the admin account if it is configured and does not exist.

    Returns a process exit code. Three cases, answered differently on
    purpose:

    Neither variable set - skip, and succeed. This runs on every
    `docker compose up`, and a fresh clone has no .env. Failing there would
    mean the stack cannot start until somebody configures an account they may
    not want, which is the opposite of what one-command setup is for.

    One set and not the other - fail. That is not a choice, it is a typo:
    ADMIN_EMIAL, or a variable that never reached the container. Skipping
    quietly is how somebody sets ADMIN_PASSWORD, sees no error, and spends
    twenty minutes wondering why they cannot log in.

    Both set - create it, or report that the address is taken and change
    nothing.
    """
    settings = get_settings()
    email = (settings.admin_email or "").strip().lower()
    password = settings.admin_password or ""

    if not email and not password:
        print("ADMIN_EMAIL not set - no admin created.")
        return 0

    if not email or not password:
        missing = "ADMIN_EMAIL" if not email else "ADMIN_PASSWORD"
        print(f"{missing} is missing. Set both or neither.", file=sys.stderr)
        return 1

    if len(password) < MIN_PASSWORD_LENGTH:
        print(
            f"ADMIN_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters.",
            file=sys.stderr,
        )
        return 1

    async with SessionFactory() as session:
        users = UserRepository(session)

        existing = await users.get_by_email(email)
        if existing is not None:
            # Deliberately not promoting them. Silently turning an existing
            # student into an admin because somebody reused an address is
            # surprising, and surprising is the wrong quality in the one
            # command that creates privileged accounts.
            print(f"{email} already exists - nothing to do.")
            return 0

        await users.add(
            User(
                email=email,
                password=await hash_password(password),
                full_name="Administrator",
                roles=[ROLE_ADMIN],
            )
        )
        # The repository flushes and never commits - that boundary belongs to
        # whoever called it. There is no request here, so this is that caller.
        await session.commit()

    # The address, never the password. This output lands in deployment logs.
    print(f"Created admin {email}.")
    return 0


async def _main() -> int:
    try:
        return await seed_admin()
    finally:
        # Close the pooled connections rather than leaving Postgres holding
        # them until it notices the process is gone.
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
