"""What the dispatcher is allowed to know about sending an event.

Three names, and they are deliberately the whole vocabulary:

    Publisher                 something that can send an event somewhere
    TransientDeliveryError    try again later
    PermanentDeliveryError    this will never work

The dispatcher never learns what Kafka is. It holds a `Publisher`, calls
`publish`, and branches on which of the two exceptions comes back. Swapping
Kafka for RabbitMQ, or adding a second destination, is a new file in `infra/`
and no change here or in the dispatcher.

That boundary was not the first design. An earlier version tried to decide
transient-versus-permanent inside the dispatcher by reading Kafka error codes,
which meant the dispatcher had a growing table of another system's vocabulary
in it - and would need a second table for the next system. The version after
that tried to avoid error codes entirely by inferring health from whether
*other* events in the same batch had succeeded, which needed a three-method
protocol and per-destination grouping to work at all.

Both were solving the problem in the wrong place. The thing that knows what a
Kafka error means is the Kafka wrapper. It translates, and everything upstream
reads two words. See docs/DESIGN.md section 3c.
"""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from smartcourse.infra.db.models.event import OutboxEvent


class DeliveryError(Exception):
    """Sending an event did not work.

    Not a `DomainError`, and the distinction matters. A DomainError is an
    expected *business* outcome - the course is full, that email is taken -
    which the API layer turns into a status code and shows to a caller. A
    delivery failure has no caller: it happens in a background loop, minutes
    or hours after the request that caused the event returned 201.

    So there is nothing to translate it into, and putting it in the domain's
    exception tree would offer the API layer a status code for something it
    can never be asked about.
    """


class TransientDeliveryError(DeliveryError):
    """The world is wrong, not the message. Trying again can work.

    A broker that is down, a connection that dropped, a leader election in
    progress, a request that timed out - and, importantly, *anything the
    wrapper does not recognise*.

    Unrecognised errors land here rather than in the permanent case because
    the two mistakes do not cost the same:

        called transient, really permanent   it retries, is abandoned after
                                             24 hours, and shows up in
                                             failed_events. Late, but correct
        called permanent, really transient   a real event is thrown away

    One of those is an inconvenience and the other is data loss, so the
    default goes to the inconvenient one. A misconfigured ACL is the case
    worth picturing: treated as transient it retries while the hourly warning
    fires, a human fixes the ACL, and the backlog drains. Treated as permanent
    it would have killed every event raised during the outage.
    """


class PermanentDeliveryError(DeliveryError):
    """Something about *this message* is wrong. Retrying cannot help.

    A payload too large for the broker's limit, a value that will not
    serialise, a malformed record. The same bytes will fail the same way in
    thirty minutes and in thirty hours, so the dispatcher stops immediately
    rather than spending a day's retries proving it.

    The row is abandoned on the spot and a `FailedEvent` records why. That is
    the only path that gives up without waiting out the 24-hour limit.
    """


class Publisher(Protocol):
    """Anything that can take an outbox row and deliver it.

    One method, and it used to be three. `is_healthy()` and
    `failure_domain()` existed only so the dispatcher could *guess* whether a
    service was down by watching which sends succeeded together. Once the
    wrapper answers that question directly by raising the right exception,
    there is nothing left to guess and the extra methods have no callers.

    A `Protocol` rather than an abstract base class, so a publisher is
    anything with the right shape - no inheritance, no registration, and a
    test can pass a small fake without importing anything from `infra/`. It is
    not `runtime_checkable`, because nothing does an isinstance check on it;
    mypy verifies the shape at the one place a real publisher is constructed.
    """

    async def publish(
        self, events: Sequence[OutboxEvent]
    ) -> dict[UUID, DeliveryError | None]:
        """Deliver a batch, and report on each event separately.

        Takes a batch rather than one event because the transports worth
        using are all batch-shaped. Kafka's `produce()` only queues a message
        and returns; the waiting happens once, in `flush()`. So a hundred
        events cost roughly one round trip, where a hundred separate calls
        would cost a hundred.

        Returns one entry per event, keyed by `OutboxEvent.id`:

            None                       sent, durably
            TransientDeliveryError     try this one again later
            PermanentDeliveryError     this one will never be deliverable

        Errors are *returned*, not raised, which is the one odd thing about
        this signature. A batch has no single outcome - ninety-eight can
        succeed while two fail for different reasons - and an exception can
        only describe one. (`asyncio.gather(return_exceptions=True)` makes
        the same trade for the same reason.)

        Anything that *is* raised from here is therefore a bug, not a
        delivery failure, and must be allowed to crash rather than be
        mistaken for "Kafka is having a moment" and retried for a day.

        A `None` is a promise that the destination has the message durably -
        for Kafka, that every in-sync replica has it. It is what lets the
        dispatcher write `published_at`, and that column is only worth having
        if it means something.

        Takes whole rows rather than their parts because a row already
        carries everything any destination could need - `topic`,
        `partition_key`, and the full envelope in `payload` - and a wrapper
        for a different transport will want different fields from the same
        row.
        """
        ...
