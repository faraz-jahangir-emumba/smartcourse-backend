"""The loop that drains the outbox. FR-16, FR-17, FR-18.

Everything else in Module 3 Area B exists so that this file can be short and
boring. The outbox guarantees an event was written; the trigger says when one
arrives; the repository knows which rows are sendable and in what order; the
publisher turns one destination's errors into two words. What is left is the
decision tree, and it fits on a page:

    sent                       mark it published
    permanent error            write a FailedEvent and stop trying
    transient error            count it, and try again later
    anything published         pull the whole backlog forward and go again
    unsent for an hour         warn
    unsent for a day           give up

Nothing here names Kafka, or knows what a partition is. Swapping the
destination is a different `Publisher` passed in from the entry point.

See docs/DESIGN.md section 3c.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse import events
from smartcourse.infra.db.models.event import OutboxEvent
from smartcourse.infra.db.repositories import (
    FailedEventRepository,
    OutboxRepository,
)
from smartcourse.services.publisher import (
    DeliveryError,
    PermanentDeliveryError,
    Publisher,
)

logger = logging.getLogger(__name__)

# A session per pass, opened by the dispatcher rather than handed to it.
# There is no request here to own a transaction, which is the same situation
# the Temporal activities are in - see workflows/activities.py.
SessionMaker = Callable[[], AsyncSession]

# 2 ** 30 seconds is thirty-four years, so the exponent is clamped well below
# anything meaningful. Not for correctness - min() would give the right answer
# either way - but so a row with a wild attempts count cannot make Python
# compute a number with a million digits before throwing it away.
_MAX_EXPONENT = 30


def backoff_delay(attempts: int, *, cap_seconds: int) -> int:
    """How long to wait before trying an event again.

    Doubling, then flat: 1, 2, 4, 8, 16, 32 … up to the cap, and the cap
    thereafter for as long as the failure lasts.

    `attempts` is the count *before* this failure, so a first failure waits
    one second. Doubling means a blip costs a second rather than a cap's
    worth of delay, while a real outage settles quickly into a slow, cheap
    poll.

    The cap is what decides recovery time: whatever it is, a service that
    comes back is noticed within that long. It is per event type - see
    events.BACKOFF_CAP_SECONDS - though in practice the shortest cap among
    the live rows decides for everyone, because the first success pulls the
    whole backlog forward.
    """
    return min(2 ** min(attempts, _MAX_EXPONENT), cap_seconds)


@dataclass(frozen=True)
class PassOutcome:
    """What one pass did. Returned so the loop can decide whether to go again."""

    claimed: int
    published: int
    abandoned: int
    rescheduled: int
    batch_size: int

    @property
    def should_continue(self) -> bool:
        """Whether to run another pass immediately rather than sleep.

        Two reasons to go again, and the first is the one that is easy to
        miss.

        Publishing something *releases ordering keys*. A chain of three
        events on one enrollment comes out one per pass, by design - so a
        pass that returned a single row out of a hundred has very likely
        unblocked the next one. Sleeping on a partial batch would leave that
        second event waiting for a NOTIFY that is never coming, because
        nothing new is being inserted; it would sit until the next tick.

        The second is the ordinary one: a full batch means there is probably
        more behind it.

        This cannot spin. A pass that publishes nothing and claims less than
        a full batch stops; a pass whose events all fail has pushed their
        `next_attempt_at` into the future, so the next claim returns
        different rows, and eventually fewer than a full batch.
        """
        return self.published > 0 or self.claimed >= self.batch_size


async def run_pass(
    session: AsyncSession,
    publisher: Publisher,
    *,
    batch_size: int,
    failure_source: str,
) -> PassOutcome:
    """Claim a batch, send it, and record what happened. One transaction.

    One transaction for the whole pass, and that is what makes a crash safe.
    The rows are locked by the claim and stay locked until the commit, so a
    dispatcher killed half way through leaves no half-updated state - the
    lock dies with the transaction and every row is simply still sendable.
    The cost is that a message may have reached Kafka without us recording
    it, which is at-least-once and why `event_id` travels in the envelope.
    """
    outbox = OutboxRepository(session)
    failures = FailedEventRepository(session)

    claimed = await outbox.claim_due(limit=batch_size)
    if not claimed:
        return PassOutcome(0, 0, 0, 0, batch_size=batch_size)

    verdicts = await publisher.publish(claimed)

    published_ids = [event.id for event in claimed if verdicts[event.id] is None]

    # Before any rescheduling, and the order matters.
    #
    # clear_backoff moves every row whose next_attempt_at is in the *future*
    # back to now. Every row in this batch was claimed because its schedule
    # had already come round, so none of them is in the future and none is
    # touched. Run it after the reschedules below and it would immediately
    # undo the backoff just applied to the events that failed in this very
    # pass.
    if published_ids:
        moved = await outbox.clear_backoff()
        if moved:
            logger.info(
                "a send succeeded, so %d waiting event(s) were pulled forward",
                moved,
            )

    abandoned = rescheduled = 0
    for event in claimed:
        verdict = verdicts[event.id]
        if verdict is None:
            continue
        if isinstance(verdict, PermanentDeliveryError):
            await _give_up(outbox, failures, event, verdict, source=failure_source)
            abandoned += 1
        else:
            await _try_later(outbox, event, verdict)
            rescheduled += 1

    await outbox.mark_published(published_ids)

    return PassOutcome(
        claimed=len(claimed),
        published=len(published_ids),
        abandoned=abandoned,
        rescheduled=rescheduled,
        batch_size=batch_size,
    )


async def _try_later(
    outbox: OutboxRepository, event: OutboxEvent, error: DeliveryError
) -> None:
    """A transient failure. Count it and push the schedule out."""
    delay = backoff_delay(
        event.attempts, cap_seconds=events.backoff_cap_seconds(event.event_type)
    )
    logger.info(
        "%s %s not sent, retrying in %ds (attempt %d): %s",
        event.event_type,
        event.id,
        delay,
        event.attempts + 1,
        error,
    )
    await outbox.reschedule(event.id, delay_seconds=delay)


async def _give_up(
    outbox: OutboxRepository,
    failures: FailedEventRepository,
    event: OutboxEvent,
    error: BaseException,
    *,
    source: str,
) -> None:
    """Stop trying, and leave a record of why.

    Both halves in one transaction. A FailedEvent without the abandonment
    would be written again on every pass; an abandonment without the
    FailedEvent would lose the only explanation anyone gets.
    """
    logger.error(
        "%s %s abandoned after %d attempt(s): %s",
        event.event_type,
        event.id,
        event.attempts + 1,
        error,
    )
    failures.add_from_outbox(event, source=source, error=str(error))
    await outbox.abandon(event.id)


async def run_sweeps(
    session: AsyncSession,
    *,
    warn_after_hours: int,
    abandon_after_hours: int,
    failure_source: str,
) -> int:
    """The two age checks. Returns how many events were given up on.

    Once per tick rather than once per pass. A pass can run many times a
    second while a backlog drains, and the same warning logged on each of
    them is a warning nobody reads.

    Giving up runs first, so the warning below does not also list the rows
    that were just abandoned.

    The warning is a log line and nothing more. Nothing pages a human until
    Area F turns this into a Prometheus gauge with an alert on it, and that
    gap is deliberate rather than overlooked - it is recorded in
    docs/DESIGN.md and in STATE.md's Concerns.
    """
    outbox = OutboxRepository(session)
    failures = FailedEventRepository(session)

    expired = await outbox.list_older_than(hours=abandon_after_hours)
    for event in expired:
        await _give_up(
            outbox,
            failures,
            event,
            TimeoutError(f"unsent for over {abandon_after_hours}h"),
            source=failure_source,
        )

    stale = await outbox.list_older_than(hours=warn_after_hours)
    if stale:
        logger.warning(
            "%d event(s) unsent for over %dh; oldest is %s %s from %s",
            len(stale),
            warn_after_hours,
            stale[0].event_type,
            stale[0].id,
            stale[0].created_at.isoformat(),
        )

    return len(expired)


async def drain(
    session_maker: SessionMaker,
    publisher: Publisher,
    *,
    batch_size: int,
    failure_source: str,
) -> None:
    """Run passes until there is nothing sendable left."""
    while True:
        async with session_maker() as session:
            outcome = await run_pass(
                session,
                publisher,
                batch_size=batch_size,
                failure_source=failure_source,
            )
            await session.commit()

        if outcome.claimed:
            logger.info(
                "pass: claimed %d, published %d, retrying %d, abandoned %d",
                outcome.claimed,
                outcome.published,
                outcome.rescheduled,
                outcome.abandoned,
            )
        if not outcome.should_continue:
            return


async def run_forever(
    session_maker: SessionMaker,
    publisher: Publisher,
    *,
    wake: asyncio.Event,
    stop: asyncio.Event,
    batch_size: int,
    tick_seconds: float,
    warn_after_hours: int,
    abandon_after_hours: int,
    failure_source: str,
) -> None:
    """Drain, sleep, repeat, until asked to stop.

    `wake` is set by whatever is listening for new events - in production a
    Postgres LISTEN connection, in a test an `asyncio.Event` someone sets by
    hand. The loop does not know which, and deliberately so: it keeps
    asyncpg out of a service module, and makes the whole loop testable
    without a notification mechanism at all.

    The flag is only ever "there might be work". It is cleared before
    draining rather than after, so an event inserted *while* a pass is
    running sets it again and earns another drain, instead of being missed
    because it arrived in the window between the query and the clear.

    `stop` is set by the entry point's signal handler. It is checked here
    between passes, which is the only safe place: a pass is one transaction
    and interrupting it mid-flight would mean sending messages we never
    recorded.
    """
    logger.info(
        "dispatcher starting: batch %d, tick %.0fs, warn %dh, give up %dh",
        batch_size,
        tick_seconds,
        warn_after_hours,
        abandon_after_hours,
    )

    sweep_now = True
    while not stop.is_set():
        if sweep_now:
            async with session_maker() as session:
                given_up = await run_sweeps(
                    session,
                    warn_after_hours=warn_after_hours,
                    abandon_after_hours=abandon_after_hours,
                    failure_source=failure_source,
                )
                await session.commit()
            if given_up:
                logger.error(
                    "gave up on %d event(s) older than %dh",
                    given_up,
                    abandon_after_hours,
                )

        wake.clear()
        await drain(
            session_maker,
            publisher,
            batch_size=batch_size,
            failure_source=failure_source,
        )

        sweep_now = await _sleep_until_woken(wake, stop, tick_seconds)

    logger.info("dispatcher stopped")


async def _sleep_until_woken(
    wake: asyncio.Event, stop: asyncio.Event, tick_seconds: float
) -> bool:
    """Wait for a notification, a shutdown, or the tick. True if it was the tick.

    Three things to watch and no busy-waiting: each is a task, the first to
    finish wins, and the rest are cancelled. A plain `sleep(tick)` would
    make a NOTIFY wait out the remainder of the interval, which is the
    latency the trigger exists to remove.
    """
    waiters = [
        asyncio.create_task(wake.wait()),
        asyncio.create_task(stop.wait()),
    ]
    done, pending = await asyncio.wait(
        waiters, timeout=tick_seconds, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    # Nothing finished, so the timeout is what ended the wait - which is the
    # tick, and the only thing that should trigger a sweep.
    return not done
