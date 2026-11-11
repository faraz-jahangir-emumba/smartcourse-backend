"""Writes to the failed_events table."""

from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse.infra.db.models.event import FailedEvent, OutboxEvent


class FailedEventRepository:
    """Where background work that could not be done is kept.

    Part A lists "Failed Events / Workflow Issues" among the nine analytics
    metrics, so a failure needs somewhere to live rather than a log line that
    scrolls away. The table was modelled in Module 1 for exactly this and has
    been unused since; this is its first writer.

    Deliberately separate from OutboxRepository even though the dispatcher
    uses both in one transaction. They are two tables answering two
    questions - "what still needs sending?" and "what went wrong, and has a
    human dealt with it?" - and `failed_events` has a `status` an admin can
    change. Letting the dispatcher's selection depend on a column a human
    edits is the kind of coupling that is invisible until someone clicks
    something.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add_from_outbox(
        self, event: OutboxEvent, *, source: str, error: str
    ) -> None:
        """Record that this event is never going to be delivered.

        Staged, not flushed, for the same reason `OutboxRepository.add` is:
        this row and the matching `abandoned_at` are one decision, and must
        land in one commit. A FailedEvent without the abandonment would
        reappear every pass; an abandonment without the FailedEvent would
        lose the reason.

        `source` is passed in rather than hardcoded. The dispatcher is
        written not to know what it is publishing to - that is the whole
        point of the Publisher protocol - so the entry point, which builds
        the concrete publisher, is what names it. The column is constrained
        to FAILURE_SOURCES, so a wrong value fails loudly at the database
        rather than quietly skewing a dashboard.

        `retry_count` carries the outbox row's `attempts` across, which is
        the one number that says whether this died on its first try or after
        a day of them.
        """
        self._session.add(
            FailedEvent(
                source=source,
                # The topic, queue or task name - whatever identifies the
                # specific piece of work within its mechanism.
                source_name=event.topic,
                # The whole envelope, so the event can be understood, or
                # replayed, without joining back to a row that may have been
                # pruned by then.
                payload=event.payload,
                error=error,
                retry_count=event.attempts,
            )
        )
