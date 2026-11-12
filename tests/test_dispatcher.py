"""The outbox dispatcher. FR-16, FR-17, FR-18.

Three groups of tests, and they cost very different amounts.

**The pure ones** - `backoff_delay` and `PassOutcome.should_continue` - touch
nothing. They are two small decisions that the rest of the loop is built on,
and both have been wrong at some point in this module's life, so they are
tested where they are cheapest to test.

**The pass tests** run against the real database, through the real
repositories, with a fake publisher. That split is deliberate: every bug this
module actually had was in SQL - a predicate, a clock, an ordering comparison
- and a mocked repository would have reproduced none of them. What Kafka is
not needed for is deciding *whether* something was delivered; the publisher
is asked, and a fake can answer.

**The loop tests** drive `drain` and `run_forever`, which own their own
sessions in production. See `_PassSession` below for how they are kept inside
the test's transaction.

A note on the clock, because it is the single most confusing thing here.
Inside one transaction Postgres's `now()` is frozen at the transaction's start
time, and the whole suite runs in one transaction per test. That turns out to
be a gift rather than a hazard: `claim_due` asks "is next_attempt_at <= now()"
and `reschedule` writes "now() + delay", so both sides are frozen to the same
instant and a one-second backoff is reliably in the future no matter how long
the test takes. There is no wall clock in these assertions and so nothing to
be flaky about.

`created_at` is the exception - it is `clock_timestamp()`, the real clock, read
per statement - which is why the helper below flushes each row separately. Two
rows added and flushed together could share a timestamp, and the ordering
guard compares with a strict `<`.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse import events
from smartcourse.infra.db.models.event import FailedEvent, OutboxEvent
from smartcourse.services.dispatcher import (
    PassOutcome,
    backoff_delay,
    drain,
    run_forever,
    run_pass,
    run_sweeps,
)
from smartcourse.services.publisher import (
    DeliveryError,
    PermanentDeliveryError,
    TransientDeliveryError,
)

SOURCE = "kafka"

DISPATCHER_LOGGER = "smartcourse.services.dispatcher"


@pytest.fixture
def dispatcher_logs(caplog):
    """`caplog`, but with the dispatcher's logger switched back on.

    Nothing the dispatcher logs is captured without this, and the reason is
    two layers away. `conftest.test_database` runs the migrations in-process,
    Alembic's `env.py` calls `logging.config.fileConfig`, and `fileConfig`
    defaults to `disable_existing_loggers=True` - so every logger that
    already exists at that moment is switched off, this module's included.

    One session-scoped fixture therefore silences the application's logging
    for the whole run. Harmless for every other test, because no other test
    asserts on a log line; it is fixable at the source by passing
    `disable_existing_loggers=False` in `env.py`, which is a change to the
    migration setup rather than to the dispatcher and is recorded in
    STATE.md's Concerns instead of being made here.

    The flag is restored afterwards so this fixture cannot change what any
    later test sees.
    """
    logger = logging.getLogger(DISPATCHER_LOGGER)
    was_disabled = logger.disabled
    logger.disabled = False
    try:
        yield caplog
    finally:
        logger.disabled = was_disabled


# --- the fake destination --------------------------------------------------


class FakePublisher:
    """A `Publisher` that answers from a rule instead of from a broker.

    Satisfies the protocol structurally - no base class, no registration,
    which is the reason `Publisher` is a `Protocol` and not an ABC.

    `decide` is given one event and returns what happened to it: `None` for
    delivered, or a `DeliveryError` subclass. Per event rather than per batch,
    because the interesting cases are the mixed ones - the protocol exists to
    let ninety-eight succeed while two fail differently, and a fake that could
    only answer for a whole batch would not exercise that at all.
    """

    def __init__(
        self,
        decide: Callable[[OutboxEvent], DeliveryError | None] | None = None,
        *,
        on_publish: Callable[[list[OutboxEvent]], None] | None = None,
    ) -> None:
        self.batches: list[list[OutboxEvent]] = []
        self._decide = decide or (lambda event: None)
        # A hook for the loop tests, which need something to happen *during* a
        # pass - setting the stop flag, usually.
        self._on_publish = on_publish

    async def publish(
        self, events: Sequence[OutboxEvent]
    ) -> dict[UUID, DeliveryError | None]:
        batch = list(events)
        self.batches.append(batch)
        if self._on_publish is not None:
            self._on_publish(batch)
        return {event.id: self._decide(event) for event in batch}

    @property
    def calls(self) -> int:
        return len(self.batches)

    @property
    def seen_ids(self) -> list[UUID]:
        """Every event handed to this publisher, in the order it arrived."""
        return [event.id for batch in self.batches for event in batch]


Verdict = Callable[[OutboxEvent], DeliveryError | None]


def always(verdict: DeliveryError | None) -> Verdict:
    return lambda event: verdict


def transient(message: str = "broker unreachable") -> TransientDeliveryError:
    return TransientDeliveryError(message)


def permanent(message: str = "payload too large") -> PermanentDeliveryError:
    return PermanentDeliveryError(message)


# --- a session the loop can own without escaping the test ------------------


class _PassSession:
    """Stands in for the fresh session `drain` opens for each pass.

    `drain` and `run_forever` take a session *maker* rather than a session,
    because one pass is one transaction and the loop is what decides where
    that boundary is. In production the maker opens a real session and the
    pass commits for real.

    A test cannot let that happen: the suite's isolation is one transaction
    per test, rolled back at the end, and a real commit would leave rows
    behind for the next test to trip over. So this wraps the test's own
    session and turns `commit()` into `flush()` - the writes become visible
    to everything later in the transaction, and none of them survives it.

    `expunge_all` is the part that is easy to leave out. Each pass in
    production gets a brand new session with an empty identity map, so it
    reads every row fresh - which is what makes `_try_later` compute the
    second retry's backoff from the second attempt's count rather than the
    first's. Dropping the objects here reproduces that.

    Expunging rather than expiring, deliberately. An expired object reloads
    itself on the next attribute access, and that reload is synchronous
    database IO - which is exactly what an async session forbids, and it
    would blow up in the assertions rather than in the code under test. A
    detached object just keeps the values it already had.

    Everything else is forwarded untouched, so the repositories underneath
    are talking to a genuine AsyncSession.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> "_PassSession":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def commit(self) -> None:
        await self._session.flush()
        self._session.expunge_all()

    def __getattr__(self, name: str):
        return getattr(self._session, name)


def maker_for(session: AsyncSession) -> Callable[[], _PassSession]:
    return lambda: _PassSession(session)


# --- writing rows ----------------------------------------------------------


async def write(
    session: AsyncSession,
    event: OutboxEvent,
    *,
    created_at: datetime | None = None,
    attempts: int | None = None,
    next_attempt_at: datetime | None = None,
) -> OutboxEvent:
    """Put one event in the outbox and flush it on its own.

    One flush per row, which is not an accident. `created_at` defaults to
    `clock_timestamp()` - read per statement - so two rows flushed together
    could land on the same microsecond, and the ordering guard's strict `<`
    would then let both through. Flushing separately guarantees the order the
    test is asserting about actually exists in the table.

    The three overrides exist to stage states that a test cannot otherwise
    reach inside a single transaction: a row that is already a day old, one
    that has failed before, one whose schedule is in the future because
    something else scheduled it.
    """
    if created_at is not None:
        event.created_at = created_at
    if attempts is not None:
        event.attempts = attempts
    if next_attempt_at is not None:
        event.next_attempt_at = next_attempt_at
    session.add(event)
    await session.flush()
    return event


def enrollment_events(
    *, enrollment_id: UUID | None = None, course_id: UUID | None = None
) -> tuple[OutboxEvent, OutboxEvent, OutboxEvent]:
    """One enrollment's whole story, in the order it must be delivered.

    All three share an `ordering_key` of `enrollment_id`, which is what the
    guard holds them back on - and a `partition_key` of `course_id`, which is
    not. The difference between those two is the point of several tests
    below.
    """
    enrollment_id = enrollment_id or uuid4()
    ids = {
        "enrollment_id": enrollment_id,
        "student_id": uuid4(),
        "course_id": course_id or uuid4(),
    }
    return (
        events.student_enrolled(**ids),
        events.student_withdrew(**ids),
        events.course_completed(**ids),
    )


def a_user_event() -> OutboxEvent:
    """An event with an ordering key of its own, unrelated to anything else."""
    return events.user_registered(user_id=uuid4(), roles=["student"])


async def reload(session: AsyncSession, event: OutboxEvent) -> OutboxEvent:
    """Read a row back from the database rather than trusting the object.

    `populate_existing` is what makes this a read rather than a lookup. The
    object is already in the session's identity map, and a plain query would
    hand it straight back with whatever values it was loaded with - which is
    precisely the stale state these tests are trying to catch. This forces
    the columns to be repopulated from the row that is actually there.

    By id rather than by `session.refresh`, so it also works after a pass has
    expunged its objects.
    """
    found = await session.scalar(
        select(OutboxEvent)
        .where(OutboxEvent.id == event.id)
        .execution_options(populate_existing=True)
    )
    assert found is not None
    return found


async def failures_for(session: AsyncSession) -> list[FailedEvent]:
    rows = await session.scalars(select(FailedEvent).order_by(FailedEvent.occurred_at))
    return list(rows)


async def one_pass(
    session: AsyncSession, publisher: FakePublisher, *, batch_size: int = 100
) -> PassOutcome:
    """Run a single pass against the test's session.

    No commit, and none is needed: `run_pass` deliberately does not own one,
    so every write it makes is already visible to the rest of this
    transaction.
    """
    return await run_pass(
        session, publisher, batch_size=batch_size, failure_source=SOURCE
    )


# --- backoff ---------------------------------------------------------------


def test_backoff_doubles_from_one_second() -> None:
    """A first failure costs a second, not a cap's worth of delay.

    `attempts` is the count *before* this failure, so a row that has never
    been tried waits 2**0. That matters more than it looks: most delivery
    failures are a blip, and starting at the cap would turn a two-second
    broker hiccup into a thirty-minute delay for every event behind it.
    """
    cap = 1800
    assert [backoff_delay(n, cap_seconds=cap) for n in range(6)] == [
        1,
        2,
        4,
        8,
        16,
        32,
    ]


def test_backoff_stops_growing_at_the_cap() -> None:
    """Doubling, then flat. The cap is what decides recovery time.

    Whatever the cap is, a destination that comes back is noticed within that
    long - which is the entire reason `course.published` has a sixty-second
    one while a welcome email has thirty minutes.
    """
    assert backoff_delay(5, cap_seconds=60) == 32
    assert backoff_delay(6, cap_seconds=60) == 60
    assert backoff_delay(50, cap_seconds=60) == 60


def test_a_wild_attempt_count_does_not_compute_a_huge_number() -> None:
    """The exponent is clamped, so Python never builds the number it discards.

    `min()` alone would give the right answer - but only after evaluating
    2**4000, which is a four-digit-kilobyte integer computed to be thrown
    away. A row with a corrupted count should cost a comparison, not a
    multiplication.
    """
    assert backoff_delay(4000, cap_seconds=1800) == 1800


def test_the_cap_comes_from_the_event_type() -> None:
    """Checked here because the dispatcher looks it up per event at send time.

    Stored nowhere on the row, deliberately: a cap stamped into four thousand
    waiting rows cannot be turned during the incident it matters in.
    """
    assert events.backoff_cap_seconds(events.COURSE_PUBLISHED) == 60
    assert events.backoff_cap_seconds(events.STUDENT_ENROLLED) == 300
    assert events.backoff_cap_seconds(events.USER_REGISTERED) == 1800
    assert events.backoff_cap_seconds("something.invented") == 1800


# --- whether to go round again ---------------------------------------------


def outcome(*, claimed: int, published: int, batch_size: int = 100) -> PassOutcome:
    return PassOutcome(
        claimed=claimed,
        published=published,
        abandoned=0,
        rescheduled=claimed - published,
        batch_size=batch_size,
    )


def test_a_full_batch_means_go_again() -> None:
    """The ordinary reason: there is probably more behind it."""
    assert outcome(claimed=100, published=100).should_continue is True
    assert outcome(claimed=100, published=0).should_continue is True


def test_publishing_anything_means_go_again() -> None:
    """The regression test for the stall, and the subtler of the two rules.

    Publishing releases ordering keys. A chain of three events on one
    enrollment comes out one per pass by design, so a pass that returned a
    single row out of a hundred has very likely unblocked the next one.

    The original rule was `claimed >= batch_size` alone. It read one-of-a
    hundred as "backlog drained" and went to sleep waiting for a NOTIFY that
    was never coming, because nothing new was being inserted - so a
    three-event chain took two ticks to drain instead of milliseconds, with
    nothing logged and nothing failing.
    """
    assert outcome(claimed=1, published=1).should_continue is True


def test_a_partial_batch_with_nothing_published_stops() -> None:
    """And this is why the rule above cannot spin.

    Every event in this pass failed, which means each has had its
    `next_attempt_at` pushed into the future - so the next claim returns
    different rows, and eventually none. Nothing published and nothing left
    to claim is the one state that has to terminate.
    """
    assert outcome(claimed=3, published=0).should_continue is False
    assert outcome(claimed=0, published=0).should_continue is False


# --- one pass, the ordinary case -------------------------------------------


async def test_a_delivered_event_is_marked_published(session) -> None:
    """The happy path, end to end through the repositories.

    `published_at` is set when the destination accepted the message, not when
    anyone consumed it. The outbox answers "did we send it?" and nothing
    more.
    """
    event = await write(session, a_user_event())
    publisher = FakePublisher()

    result = await one_pass(session, publisher)

    assert (result.claimed, result.published) == (1, 1)
    assert publisher.seen_ids == [event.id]

    row = await reload(session, event)
    assert row.published_at is not None
    assert row.abandoned_at is None
    # Not counted. `attempts` is the retry exponent, and a success is not a
    # retry - a row that worked first time should read as having worked first
    # time.
    assert row.attempts == 0


async def test_an_empty_outbox_never_calls_the_publisher(session) -> None:
    """Claiming nothing returns early, before the publisher is touched.

    Worth asserting rather than assuming: this is the state the dispatcher is
    in almost all of the time, and an empty batch handed to a real producer
    would still cost a `flush()` round trip on every tick.
    """
    publisher = FakePublisher()

    result = await one_pass(session, publisher)

    assert result == PassOutcome(0, 0, 0, 0, batch_size=100)
    assert publisher.calls == 0


async def test_a_claimed_batch_stops_at_the_batch_size(session) -> None:
    """The limit is applied in SQL, not by slicing afterwards.

    Each event gets its own ordering key here, so the guard is not what is
    doing the limiting - without that, this test would pass for the wrong
    reason.
    """
    for _ in range(5):
        await write(session, a_user_event())
    publisher = FakePublisher()

    result = await one_pass(session, publisher, batch_size=2)

    assert result.claimed == 2
    assert len(publisher.batches[0]) == 2


# --- one pass, when sending fails ------------------------------------------


async def test_a_transient_failure_is_counted_and_pushed_out(session) -> None:
    """Try again later: count the attempt, move the schedule, send nothing.

    The row stays live. That is the whole difference between this and the
    permanent case - the event is still going to be delivered, just not now.
    """
    event = await write(session, a_user_event())
    publisher = FakePublisher(always(transient()))

    result = await one_pass(session, publisher)

    assert (result.published, result.rescheduled, result.abandoned) == (0, 1, 0)

    row = await reload(session, event)
    assert row.published_at is None
    assert row.abandoned_at is None
    assert row.attempts == 1
    assert row.last_attempt_at is not None
    assert not await failures_for(session)


async def test_a_rescheduled_event_is_not_claimed_again_this_pass(session) -> None:
    """The backoff is real, and it is the database that enforces it.

    A one-second delay is enough to assert on because both sides of the
    comparison are frozen: `reschedule` writes `now() + 1s` and `claim_due`
    asks `<= now()`, and inside one transaction `now()` is the same instant
    for both. No wall clock, no flake.
    """
    await write(session, a_user_event())
    publisher = FakePublisher(always(transient()))

    await one_pass(session, publisher)
    second = await one_pass(session, publisher)

    assert second.claimed == 0
    assert publisher.calls == 1


async def test_a_permanent_failure_is_abandoned_with_a_record(session) -> None:
    """This will never work, so stop now rather than spend a day proving it.

    The two halves are one decision and land in one transaction: a
    `FailedEvent` without the abandonment would be written again on every
    pass, and an abandonment without the `FailedEvent` would lose the only
    explanation anyone gets.
    """
    event = await write(session, a_user_event())
    publisher = FakePublisher(always(permanent("payload too large")))

    result = await one_pass(session, publisher)

    assert (result.published, result.rescheduled, result.abandoned) == (0, 0, 1)

    row = await reload(session, event)
    assert row.abandoned_at is not None
    assert row.published_at is None

    (failure,) = await failures_for(session)
    assert failure.source == SOURCE
    assert failure.source_name == events.TOPIC_USERS
    assert "payload too large" in failure.error
    assert failure.status == "pending"
    # The whole envelope, not just the data - so this row can be understood,
    # or replayed, without joining back to an outbox row that may be pruned
    # by then.
    assert failure.payload["event_id"] == str(event.id)
    assert failure.payload["event_type"] == events.USER_REGISTERED


async def test_the_source_is_passed_in_not_assumed(session) -> None:
    """The dispatcher never names its destination; the entry point does.

    That is what makes a second transport a new file in `infra/` rather than
    an edit here. The column is constrained to FAILURE_SOURCES, so a wrong
    value fails at the database rather than quietly skewing a dashboard.
    """
    await write(session, a_user_event())
    publisher = FakePublisher(always(permanent()))

    await run_pass(session, publisher, batch_size=10, failure_source="celery")

    (failure,) = await failures_for(session)
    assert failure.source == "celery"


async def test_a_mixed_batch_handles_each_event_separately(session) -> None:
    """Ninety-eight can succeed while two fail differently.

    This is the case the protocol's return-a-dict signature exists for: an
    exception can only describe one outcome, and a batch has three here.
    Separate ordering keys, so all three are claimable at once.
    """
    good = await write(session, a_user_event())
    flaky = await write(session, a_user_event())
    broken = await write(session, a_user_event())

    verdicts = {flaky.id: transient(), broken.id: permanent()}
    publisher = FakePublisher(lambda event: verdicts.get(event.id))

    result = await one_pass(session, publisher)

    assert result.claimed == 3
    assert (result.published, result.rescheduled, result.abandoned) == (1, 1, 1)

    assert (await reload(session, good)).published_at is not None
    assert (await reload(session, flaky)).attempts == 1
    assert (await reload(session, broken)).abandoned_at is not None
    assert len(await failures_for(session)) == 1


async def test_the_failure_record_carries_the_count_before_this_attempt(
    session,
) -> None:
    """`retry_count` is `attempts` as it was when the event was handed over.

    Worth pinning down because it is off by one from the outbox row that
    survives it: `abandon` increments `attempts` in SQL after this row is
    staged, so an event that died on its fourth try leaves `retry_count = 3`
    next to an outbox row reading `attempts = 4`.
    """
    event = await write(session, a_user_event(), attempts=3)
    publisher = FakePublisher(always(permanent()))

    await one_pass(session, publisher)

    (failure,) = await failures_for(session)
    assert failure.retry_count == 3
    assert (await reload(session, event)).attempts == 4


# --- the ordering guard ----------------------------------------------------


async def test_one_enrollments_events_go_out_one_at_a_time(session) -> None:
    """The guard: skip a row if an older live row shares its ordering key.

    Without it, `next_attempt_at` would break FIFO and a consumer could see
    someone leave a course they never joined - because the enrollment is
    backing off while the withdrawal is due now.

    A consequence the loop depends on: a batch can never contain two events
    with the same ordering key, so nothing inside a batch can overtake
    anything else.
    """
    enrolled, withdrew, completed = enrollment_events()
    for event in (enrolled, withdrew, completed):
        await write(session, event)

    publisher = FakePublisher()

    first = await one_pass(session, publisher)
    assert first.claimed == 1
    assert publisher.batches[0][0].id == enrolled.id

    second = await one_pass(session, publisher)
    assert second.claimed == 1
    assert publisher.batches[1][0].id == withdrew.id

    third = await one_pass(session, publisher)
    assert third.claimed == 1
    assert publisher.batches[2][0].id == completed.id


async def test_a_stuck_enrollment_does_not_block_another_student(session) -> None:
    """The reason `ordering_key` is not `partition_key`.

    Both of these are in the same course, so they share a partition key -
    which is what a per-course consumer wants. But Ali's enrollment and
    Sara's have no ordering relationship at all, and holding Sara back
    because Ali's event is stuck would be a self-inflicted outage on an
    unrelated student.

    This test is the one that fails if the two keys are ever conflated again.
    """
    course_id = uuid4()
    ali_enrolled, ali_withdrew, _ = enrollment_events(course_id=course_id)
    sara_enrolled, _, _ = enrollment_events(course_id=course_id)

    await write(session, ali_enrolled)
    await write(session, ali_withdrew)
    await write(session, sara_enrolled)

    # The same partition, so the keys really are shared.
    assert ali_enrolled.partition_key == sara_enrolled.partition_key
    assert ali_enrolled.ordering_key != sara_enrolled.ordering_key

    publisher = FakePublisher(
        lambda event: transient() if event.id == ali_enrolled.id else None
    )

    result = await one_pass(session, publisher)

    # Ali's first event and Sara's, but not Ali's second.
    assert result.claimed == 2
    assert set(publisher.seen_ids) == {ali_enrolled.id, sara_enrolled.id}
    assert (await reload(session, sara_enrolled)).published_at is not None
    assert (await reload(session, ali_withdrew)).published_at is None


async def test_a_backing_off_event_holds_back_the_one_behind_it(session) -> None:
    """The guard is about live rows, not due ones - which is the whole point.

    After this pass the enrollment is scheduled a second out and the
    withdrawal is due now. FIFO alone would send the withdrawal. The guard
    sees an older *live* row on the key and holds it.
    """
    enrolled, withdrew, _ = enrollment_events()
    await write(session, enrolled)
    await write(session, withdrew)

    publisher = FakePublisher(always(transient()))
    await one_pass(session, publisher)

    second = await one_pass(session, publisher)

    assert second.claimed == 0
    assert (await reload(session, withdrew)).attempts == 0


async def test_abandoning_an_event_releases_the_key_behind_it(session) -> None:
    """Giving up unjams the chain, and that is a deliberate trade.

    A consumer may now receive a withdrawal whose enrollment event was
    abandoned, and has to tolerate it. The alternative - holding the rest
    forever - jams the key permanently and loses a true fact as well as the
    broken one.
    """
    enrolled, withdrew, _ = enrollment_events()
    await write(session, enrolled)
    await write(session, withdrew)

    publisher = FakePublisher(
        lambda event: permanent() if event.id == enrolled.id else None
    )

    first = await one_pass(session, publisher)
    assert first.abandoned == 1

    second = await one_pass(session, publisher)
    assert second.published == 1
    assert (await reload(session, withdrew)).published_at is not None


async def test_a_published_event_releases_the_key_behind_it(session) -> None:
    """The ordinary release, and what makes `should_continue` worth having."""
    enrolled, withdrew, _ = enrollment_events()
    await write(session, enrolled)
    await write(session, withdrew)

    publisher = FakePublisher()
    await one_pass(session, publisher)
    second = await one_pass(session, publisher)

    assert second.published == 1
    assert publisher.seen_ids == [enrolled.id, withdrew.id]


# --- one success pulls the backlog forward ---------------------------------


async def test_a_success_makes_every_waiting_row_due(session) -> None:
    """The cheapest useful thing in the dispatcher.

    One success proves the destination is up, which makes every other row's
    schedule stale information - those delays were set because something was
    broken, and it demonstrably is not any more. Without this a two-hour
    outage would end with four thousand events each serving out their own
    thirty minutes.
    """
    waiting = await write(
        session,
        a_user_event(),
        attempts=4,
        next_attempt_at=datetime.now(UTC) + timedelta(hours=1),
    )
    fresh = await write(session, a_user_event())

    publisher = FakePublisher()

    first = await one_pass(session, publisher)
    # Only the fresh one was due.
    assert first.claimed == 1
    assert publisher.seen_ids == [fresh.id]

    # And now the waiting one is, because the fresh one got through.
    second = await one_pass(session, publisher)
    assert second.published == 1
    assert (await reload(session, waiting)).published_at is not None


async def test_a_pass_that_publishes_nothing_leaves_the_backlog_alone(
    session,
) -> None:
    """No success, no evidence the destination is up, no reason to retry early."""
    waiting = await write(
        session,
        a_user_event(),
        attempts=4,
        next_attempt_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await write(session, a_user_event())

    publisher = FakePublisher(always(transient()))
    await one_pass(session, publisher)

    second = await one_pass(session, publisher)
    assert second.claimed == 0
    assert (await reload(session, waiting)).published_at is None


async def test_a_never_attempted_future_row_is_not_pulled_forward(session) -> None:
    """The `attempts > 0` guard, and the second place the frozen clock bit.

    `clear_backoff` used to select on `next_attempt_at > now()` alone. Inside
    the dispatcher's transaction `now()` is the transaction's *start*, so a
    row inserted by some other transaction *during* the pass is "in the
    future" by that measure and was being swept up as though it were backing
    off.

    Harmless in outcome - the row was due anyway - but it meant an extra
    UPDATE per arriving event under load, and a log line claiming a backlog
    had been rescued when nothing was waiting. "Has actually failed at least
    once" is exact, and has no clock in it.

    The row below is exactly that case: future-dated, never attempted.
    """
    never_tried = await write(
        session,
        a_user_event(),
        attempts=0,
        next_attempt_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await write(session, a_user_event())

    publisher = FakePublisher()
    await one_pass(session, publisher)

    second = await one_pass(session, publisher)
    assert second.claimed == 0
    assert (await reload(session, never_tried)).published_at is None


async def test_a_success_does_not_undo_the_backoff_set_in_the_same_pass(
    session,
) -> None:
    """Order of operations inside `run_pass`, and it is load-bearing.

    `clear_backoff` runs *before* the reschedules. Every row in the batch was
    claimed because its schedule had already come round, so none of them is
    in the future and none is touched. Run it afterwards and it would
    immediately undo the backoff just applied to the events that failed in
    this very pass - which would retry a failing event on every pass, for as
    long as anything else was succeeding.
    """
    good = await write(session, a_user_event())
    flaky = await write(session, a_user_event())

    publisher = FakePublisher(
        lambda event: transient() if event.id == flaky.id else None
    )

    first = await one_pass(session, publisher)
    assert (first.published, first.rescheduled) == (1, 1)
    assert (await reload(session, good)).published_at is not None

    second = await one_pass(session, publisher)
    assert second.claimed == 0


# --- the age sweeps --------------------------------------------------------


async def test_an_event_unsent_for_a_day_is_given_up_on(session) -> None:
    """The 24-hour rule, which replaced counting attempts.

    Retry delays double, so ten attempts elapse in about seventeen minutes -
    meaning a twenty-minute outage would have marked the whole backlog as
    given up while the policy claimed a day. Age is the thing the policy is
    actually about, so age is what is measured.
    """
    old = await write(
        session, a_user_event(), created_at=datetime.now(UTC) - timedelta(hours=30)
    )
    recent = await write(session, a_user_event())

    given_up = await run_sweeps(
        session, warn_after_hours=1, abandon_after_hours=24, failure_source=SOURCE
    )

    assert given_up == 1
    assert (await reload(session, old)).abandoned_at is not None
    assert (await reload(session, recent)).abandoned_at is None

    (failure,) = await failures_for(session)
    assert "unsent for over 24h" in failure.error
    assert failure.source == SOURCE


async def test_an_event_past_the_warning_but_not_the_limit_is_left_alone(
    session,
) -> None:
    """Warning and giving up are two numbers on one sweep, not one decision.

    At one hour something is wrong and a human should look. At twenty-four
    the event is discarded. Nothing between them touches the row.
    """
    stale = await write(
        session, a_user_event(), created_at=datetime.now(UTC) - timedelta(hours=5)
    )

    given_up = await run_sweeps(
        session, warn_after_hours=1, abandon_after_hours=24, failure_source=SOURCE
    )

    assert given_up == 0
    row = await reload(session, stale)
    assert row.abandoned_at is None
    assert row.published_at is None
    assert not await failures_for(session)


async def test_the_sweep_warns_about_what_it_did_not_abandon(
    session, dispatcher_logs
) -> None:
    """Giving up runs first, so the warning does not list what it just killed.

    A subtle one: both halves call `list_older_than`, and if the order were
    reversed the operator's warning would name events that no longer need
    attention.
    """
    await write(
        session, a_user_event(), created_at=datetime.now(UTC) - timedelta(hours=30)
    )
    await write(
        session, a_user_event(), created_at=datetime.now(UTC) - timedelta(hours=5)
    )

    with dispatcher_logs.at_level(logging.WARNING, logger=DISPATCHER_LOGGER):
        given_up = await run_sweeps(
            session, warn_after_hours=1, abandon_after_hours=24, failure_source=SOURCE
        )

    assert given_up == 1
    warnings = [
        record
        for record in dispatcher_logs.records
        if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    # One left to warn about, not two: the thirty-hour row was abandoned
    # before the warning went looking.
    assert warnings[0].args[0] == 1


async def test_a_published_event_is_never_swept(session) -> None:
    """Both sweeps are about live rows. A sent event is simply history.

    This is also what makes marking rows rather than deleting them free: a
    row drops out of both partial indexes the moment it is published.
    """
    sent = await write(
        session, a_user_event(), created_at=datetime.now(UTC) - timedelta(hours=30)
    )
    await one_pass(session, FakePublisher())

    given_up = await run_sweeps(
        session, warn_after_hours=1, abandon_after_hours=24, failure_source=SOURCE
    )

    assert given_up == 0
    assert (await reload(session, sent)).abandoned_at is None
    assert not await failures_for(session)


# --- draining --------------------------------------------------------------


async def test_drain_empties_a_chain_in_one_call(session) -> None:
    """The stall regression test, at the level where it actually bit.

    Three events on one ordering key come out one per pass - the guard makes
    sure of it - so `drain` has to keep going on a batch of one out of ten.
    Under the original `claimed >= batch_size` rule this returned after the
    first pass with two events still sitting there, and nothing said so.

    Four passes: three that publish one each, and one that finds nothing.
    """
    enrolled, withdrew, completed = enrollment_events()
    for event in (enrolled, withdrew, completed):
        await write(session, event)

    publisher = FakePublisher()

    await drain(maker_for(session), publisher, batch_size=10, failure_source=SOURCE)

    assert publisher.seen_ids == [enrolled.id, withdrew.id, completed.id]
    for event in (enrolled, withdrew, completed):
        assert (await reload(session, event)).published_at is not None


async def test_drain_stops_when_everything_is_failing(session) -> None:
    """The termination case, and the reason `should_continue` cannot spin.

    Every event fails, so nothing is published; each failure pushes its
    schedule out, so the next claim comes back empty. Two passes and done -
    a `drain` that kept going here would be an infinite loop hammering a
    broker that is already down.
    """
    for _ in range(3):
        await write(session, a_user_event())

    publisher = FakePublisher(always(transient()))

    await asyncio.wait_for(
        drain(maker_for(session), publisher, batch_size=10, failure_source=SOURCE),
        timeout=10,
    )

    assert publisher.calls == 1


async def test_drain_keeps_going_while_batches_are_full(session) -> None:
    """The ordinary reason to continue: a full batch means more behind it."""
    for _ in range(5):
        await write(session, a_user_event())

    publisher = FakePublisher()

    await drain(maker_for(session), publisher, batch_size=2, failure_source=SOURCE)

    # 2, 2, 1, then an empty pass that returns without publishing.
    assert [len(batch) for batch in publisher.batches] == [2, 2, 1]


async def test_drain_recomputes_the_backoff_from_the_stored_count(session) -> None:
    """Each pass reads `attempts` back from the row, not from memory.

    In production that is free, because every pass opens its own session. The
    test has to mirror it - see `_PassSession.commit` - or the second retry
    would be scheduled from the first attempt's stale count, and the backoff
    would never actually grow.
    """
    event = await write(session, a_user_event())
    publisher = FakePublisher(always(transient()))

    await drain(maker_for(session), publisher, batch_size=1, failure_source=SOURCE)

    row = await reload(session, event)
    # Claimed once, failed once. A full batch of one means drain went round
    # again; the second claim found the row scheduled out and stopped.
    assert row.attempts == 1
    assert publisher.calls == 1


# --- the long-running loop -------------------------------------------------


async def test_run_forever_returns_when_already_stopped(session) -> None:
    """`stop` is checked between passes, which is the only safe place.

    A pass is one transaction, and interrupting it mid-flight would mean
    sending messages we never recorded. Checked at the top, so a shutdown
    that arrives before the loop starts does nothing at all.
    """
    await write(session, a_user_event())

    stop = asyncio.Event()
    stop.set()
    publisher = FakePublisher()

    await asyncio.wait_for(
        run_forever(
            maker_for(session),
            publisher,
            wake=asyncio.Event(),
            stop=stop,
            batch_size=10,
            tick_seconds=60,
            warn_after_hours=1,
            abandon_after_hours=24,
            failure_source=SOURCE,
        ),
        timeout=10,
    )

    assert publisher.calls == 0


async def test_run_forever_drains_once_and_then_shuts_down(session) -> None:
    """One full turn of the loop: sweep, drain, and out.

    The stop flag is set from inside the publisher, which is the closest a
    test gets to a signal arriving mid-pass. The loop still finishes the
    transaction it is in before noticing.
    """
    event = await write(session, a_user_event())

    stop = asyncio.Event()
    publisher = FakePublisher(on_publish=lambda batch: stop.set())

    await asyncio.wait_for(
        run_forever(
            maker_for(session),
            publisher,
            wake=asyncio.Event(),
            stop=stop,
            batch_size=10,
            tick_seconds=60,
            warn_after_hours=1,
            abandon_after_hours=24,
            failure_source=SOURCE,
        ),
        timeout=10,
    )

    assert publisher.calls == 1
    assert (await reload(session, event)).published_at is not None


async def test_run_forever_clears_the_wake_flag_before_draining(session) -> None:
    """Cleared before the drain, never after, and the window is why.

    An event inserted *while* a pass is running sets the flag again and earns
    another drain. Clear it afterwards and that event would be swallowed: it
    arrived after the query that missed it and before the clear that forgot
    it, and would sit until the next tick.

    Here the flag is set before the loop starts and must be clear by the time
    the loop exits.
    """
    wake = asyncio.Event()
    wake.set()
    stop = asyncio.Event()
    await write(session, a_user_event())

    publisher = FakePublisher(on_publish=lambda batch: stop.set())

    await asyncio.wait_for(
        run_forever(
            maker_for(session),
            publisher,
            wake=wake,
            stop=stop,
            batch_size=10,
            tick_seconds=60,
            warn_after_hours=1,
            abandon_after_hours=24,
            failure_source=SOURCE,
        ),
        timeout=10,
    )

    assert not wake.is_set()


async def test_run_forever_sweeps_before_its_first_drain(session) -> None:
    """`sweep_now` starts true, so a restart inspects the backlog immediately.

    Without that, a dispatcher restarted after a long outage would wait a
    whole tick before noticing that a day's worth of events had expired.
    """
    old = await write(
        session, a_user_event(), created_at=datetime.now(UTC) - timedelta(hours=30)
    )
    fresh = await write(session, a_user_event())

    # The sweep happens first, so by the time the drain claims anything the
    # expired row is already abandoned and only the fresh one is live. Ending
    # the loop from inside the publisher means exactly one turn runs.
    stop = asyncio.Event()
    publisher = FakePublisher(on_publish=lambda batch: stop.set())

    await asyncio.wait_for(
        run_forever(
            maker_for(session),
            publisher,
            wake=asyncio.Event(),
            stop=stop,
            batch_size=10,
            tick_seconds=60,
            warn_after_hours=1,
            abandon_after_hours=24,
            failure_source=SOURCE,
        ),
        timeout=10,
    )

    assert (await reload(session, old)).abandoned_at is not None
    assert len(await failures_for(session)) == 1
    # The expired row never reached the publisher; the fresh one did.
    assert publisher.seen_ids == [fresh.id]


# --- the sleep -------------------------------------------------------------


@pytest.mark.parametrize("flag", ["wake", "stop"])
async def test_the_sleep_ends_early_when_something_happens(flag: str) -> None:
    """A notification must not wait out the rest of the interval.

    That latency is the entire reason the LISTEN/NOTIFY trigger exists - a
    plain `sleep(tick)` would make an event wait up to five minutes for a
    wake-up that had already arrived.

    False means "not the tick", which is what tells the loop not to sweep.
    """
    from smartcourse.services.dispatcher import _sleep_until_woken

    wake, stop = asyncio.Event(), asyncio.Event()
    {"wake": wake, "stop": stop}[flag].set()

    was_tick = await asyncio.wait_for(
        _sleep_until_woken(wake, stop, tick_seconds=30), timeout=5
    )

    assert was_tick is False


async def test_the_sleep_reports_the_tick_when_nothing_happens() -> None:
    """True means the timeout ended it, and the timeout is what triggers a sweep.

    Once per tick rather than once per pass: a pass can run many times a
    second while a backlog drains, and the same warning logged on each of
    them is a warning nobody reads.
    """
    from smartcourse.services.dispatcher import _sleep_until_woken

    was_tick = await asyncio.wait_for(
        _sleep_until_woken(asyncio.Event(), asyncio.Event(), tick_seconds=0.05),
        timeout=5,
    )

    assert was_tick is True
