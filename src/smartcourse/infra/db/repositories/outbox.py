"""Writes to the outbox table."""

from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse.infra.db.models.event import OutboxEvent


class OutboxRepository:
    """Where an event is put so it is guaranteed to reach Kafka.

    Thin to the point of looking pointless - one method that forwards to
    `session.add`. It earns its place by being the thing that keeps services
    away from the session.

    Every other service in this codebase takes repositories and never sees an
    AsyncSession, which is what stops a service from committing halfway
    through a business rule. Threading a raw session into five services so
    they could add an event would undo that for the sake of saving this file.

    It also leaves one place to change if writing an event ever needs to do
    more than an insert - a counter, a size check, a second destination.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, event: OutboxEvent) -> None:
        """Stage an event in the caller's transaction.

        Not async, and does not flush. Both are deliberate.

        No flush, because the whole point is that the event is written in the
        *same* commit as the business change that caused it - the caller
        decides when that is. Flushing here would send the INSERT early,
        which is harmless but suggests the event is a separate step. It is
        not: it is part of the same one.

        There is also nothing to wait for. `session.add` only puts the object
        in the session's pending set; no database call happens until a flush.
        """
        self._session.add(event)
