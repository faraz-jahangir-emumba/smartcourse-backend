"""Queries against the users table."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse.infra.db.models.user import User


class UserRepository:
    """Everything that reads or writes users.

    Takes a session rather than creating one, so every repository used during
    a request shares the same transaction. Building its own would mean a
    registration could save the user and then fail to save something related,
    with no way to undo the first part.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_email(self, email: str) -> User | None:
        """None when there is no such user - not an exception.

        Absence is an ordinary outcome here: registration *wants* it, login
        does not. Raising would force the caller that expects it to catch, so
        the decision about what absence means is left to the service.
        """
        return await self._session.scalar(select(User).where(User.email == email))

    async def get_by_id(self, user_id: UUID) -> User | None:
        # session.get rather than a select: it checks the identity map first,
        # so a user already loaded in this transaction costs no second query.
        return await self._session.get(User, user_id)

    async def add(self, user: User) -> User:
        """Insert, and return the user with database-assigned values filled in.

        flush, not commit. This sends the INSERT so Postgres applies defaults
        and enforces constraints - the caller gets back a usable object with
        its id and timestamps - while leaving the transaction open for the
        request to finish or fail as a whole.
        """
        self._session.add(user)
        await self._session.flush()
        return user
