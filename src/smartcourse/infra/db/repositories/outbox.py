"""Reads and writes to the outbox table."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from smartcourse.infra.db.models.event import OutboxEvent


class OutboxRepository:
    """Where an event is put, and where the dispatcher comes to collect it.

    Two halves with two callers. `add` is used by the five services that
    raise events, and is the whole outbox pattern at the call site. Everything
    below it is used by the dispatcher and by nothing else.

    They share a class because they are queries about one table, which is what
    a repository is for. The alternative - a second DispatcherRepository -
    would split the knowledge of what "a live row" means across two files, and
    that definition is already load-bearing in four places.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- writing an event -------------------------------------------------

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

    # --- the dispatcher ---------------------------------------------------

    async def claim_due(self, *, limit: int) -> Sequence[OutboxEvent]:
        """Take the next batch of sendable events, and lock them.

        Four conditions, and the fourth is the interesting one.

        Live: not sent, not given up on. Due: its schedule has come round.
        And then the ordering guard - skip this row if an *older* live row
        shares its ordering key.

        The guard exists because `next_attempt_at` breaks FIFO on purpose:

            10:00:00  Ali enrols     -> tried, Kafka hiccups, retry at 10:00:02
            10:00:01  Ali withdraws  -> due immediately

        Without it, the withdrawal is sent first and a consumer sees someone
        leave a course they never joined. With it, nothing for that
        enrollment moves until the enrollment event does. It compares
        `ordering_key` rather than `partition_key` so that only Ali is held
        up - Sara's enrollment in the same course is unrelated and keeps
        flowing.

        A consequence worth knowing, because the loop depends on it: a batch
        can never contain two events with the same ordering key, so nothing
        inside a batch can overtake anything else.

        FOR UPDATE locks the rows; SKIP LOCKED makes a second reader step
        over them instead of waiting. We run one dispatcher, so in practice
        nothing is skipped - this is here for the overlap during a redeploy,
        when the old process has not quite exited. The lock dies with the
        transaction, so a dispatcher that is killed mid-send strands nothing;
        that is also exactly where at-least-once delivery comes from.
        """
        older = aliased(OutboxEvent)
        has_older_sibling = (
            select(1)
            .where(
                older.published_at.is_(None),
                older.abandoned_at.is_(None),
                older.ordering_key == OutboxEvent.ordering_key,
                older.created_at < OutboxEvent.created_at,
            )
            .exists()
        )

        rows = await self._session.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.published_at.is_(None),
                OutboxEvent.abandoned_at.is_(None),
                OutboxEvent.next_attempt_at <= func.now(),
                ~has_older_sibling,
            )
            # Roughly FIFO. Only roughly, because a retried row keeps its
            # original created_at and so re-enters the queue at its old
            # position rather than at the back - which is what we want, since
            # it was first in line before it failed.
            .order_by(OutboxEvent.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return rows.all()

    async def mark_published(self, event_ids: Sequence[UUID]) -> None:
        """Record that these reached the destination durably.

        One UPDATE for the whole batch rather than one per row. The rows are
        already locked by claim_due in this same transaction, so there is
        nothing to contend with.

        `published_at` is set when the destination *accepted* the message,
        not when anyone consumed it. The outbox answers "did we send it?";
        Kafka's own offsets answer "did they read it?".
        """
        if not event_ids:
            return
        await self._session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id.in_(event_ids))
            .values(published_at=func.now())
        )

    async def reschedule(self, event_id: UUID, *, delay_seconds: int) -> None:
        """A transient failure: count it, and push the next attempt out.

        The delay is computed by the caller, which knows the attempt count
        and the per-event cap. Here it is only applied.

        Both timestamps come from `now()` in SQL rather than from Python's
        clock, and that is on purpose: `claim_due` asks the database whether
        a row is due, so the moment a row is scheduled and the moment it is
        judged due have to be measured by the same clock. A container whose
        clock has drifted a few seconds from the database's would otherwise
        schedule retries slightly in the past or the future, forever.
        """
        await self._session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(
                attempts=OutboxEvent.attempts + 1,
                last_attempt_at=func.now(),
                # make_interval takes years, months, weeks, days, hours,
                # minutes, seconds. Building the interval in SQL avoids
                # sending a Python timedelta that was computed against a
                # different clock.
                next_attempt_at=func.now()
                + func.make_interval(0, 0, 0, 0, 0, 0, delay_seconds),
            )
        )

    async def abandon(self, event_id: UUID) -> None:
        """Stop trying. The row stays, and stops being selected.

        Called for a permanent delivery error, and by the 24-hour sweeper.
        Both also write a FailedEvent, which is where the error message and
        the human-facing status live.

        Setting `abandoned_at` also releases the ordering key, so anything
        queued behind this event starts flowing again. That is deliberate and
        has a cost: a consumer may receive a withdrawal whose enrollment
        event was abandoned, and has to tolerate it. The alternative - holding
        the rest forever - jams the key permanently and loses a true fact as
        well as the broken one.
        """
        await self._session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(
                attempts=OutboxEvent.attempts + 1,
                last_attempt_at=func.now(),
                abandoned_at=func.now(),
            )
        )

    async def clear_backoff(self) -> int:
        """Make every waiting row due now. Returns how many were moved.

        Run after any successful send, and it is the cheapest useful thing in
        the dispatcher.

        One success proves the destination is up, which makes every other
        row's schedule stale information - those delays were set because
        something was broken, and it demonstrably is not any more. Without
        this, a two-hour outage would end with four thousand events each
        serving out their own thirty minutes. With it, the first row whose
        timer happens to expire acts as a probe for all of them and the
        backlog drains in one pass.

        It also means a per-event backoff cap is really a per-*system*
        recovery time: whichever live event has the shortest cap decides how
        quickly everyone finds out, which is the entire job of the short cap
        on course.published.
        """
        result = await self._session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.published_at.is_(None),
                OutboxEvent.abandoned_at.is_(None),
                # Only rows that have actually failed. A row with no attempts
                # has no backoff to clear.
                #
                # Without this the clause below catches fresh rows too, and
                # the reason is worth knowing: inside this transaction now()
                # is the transaction's *start* time, so anything inserted
                # while the pass was running is "in the future" by that
                # measure. Harmless - they were due anyway - but it is an
                # extra UPDATE per arriving event under load, and it makes
                # the log line claim a backlog was rescued when nothing was
                # waiting. Found by watching the log of a running dispatcher.
                OutboxEvent.attempts > 0,
                OutboxEvent.next_attempt_at > func.now(),
            )
            .values(next_attempt_at=func.now())
        )
        return result.rowcount

    async def list_older_than(self, *, hours: int) -> Sequence[OutboxEvent]:
        """Live rows that have been waiting longer than this. Oldest first.

        Serves two callers on the same tick, with different numbers. At one
        hour it is the warning - something is wrong and a human should look.
        At twenty-four it is the give-up sweeper, and every row it returns is
        abandoned.

        Not locked, because the one-hour caller only reads. The sweeper locks
        each row as it abandons it.
        """
        rows = await self._session.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.published_at.is_(None),
                OutboxEvent.abandoned_at.is_(None),
                OutboxEvent.created_at
                < func.now() - func.make_interval(0, 0, 0, 0, hours, 0, 0),
            )
            .order_by(OutboxEvent.created_at)
        )
        return rows.all()
