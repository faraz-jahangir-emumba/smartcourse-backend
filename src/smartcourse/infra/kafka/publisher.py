"""Sending outbox rows to Kafka.

The whole job of this file is translation. It takes a batch of rows, hands
them to librdkafka, and turns whatever comes back into the two words the rest
of the system understands - transient or permanent. Nothing above it knows
what a partition is, and nothing above it imports `confluent_kafka`.

Why the translation belongs here rather than in the dispatcher: the thing that
knows what `RECORD_LIST_TOO_LARGE` means is the Kafka wrapper. Put that
knowledge in the dispatcher and it needs a second table of error codes the day
a second transport appears, and a third after that. See docs/DESIGN.md
section 3c.
"""

import asyncio
import json
import logging
from collections.abc import Sequence
from uuid import UUID

from confluent_kafka import KafkaError, KafkaException, Producer

from smartcourse.infra.db.models.event import OutboxEvent
from smartcourse.services.publisher import (
    DeliveryError,
    PermanentDeliveryError,
    TransientDeliveryError,
)

logger = logging.getLogger(__name__)


# The errors that mean "this message will never work". Everything else -
# including everything not listed here - is transient.
#
# Four entries, and the shortness is deliberate. The two mistakes do not cost
# the same: calling something transient when it is permanent means it retries
# and is abandoned after 24 hours, which is late but correct; calling
# something permanent when it is transient throws a real event away. So the
# default goes to the recoverable mistake, and this list only grows on proof.
#
# Note what is *not* here. UNKNOWN_TOPIC_OR_PART looks permanent and is not -
# broker metadata goes stale during a restart. TOPIC_AUTHORIZATION_FAILED is
# the case worth picturing: as a transient error it retries while the hourly
# warning fires, somebody fixes the ACL, and the backlog drains. Marked
# permanent, it would have killed every event raised during the outage.
#
# An earlier design classified with `KafkaError.retriable()` instead. Checked
# before building on it: that flag is attached by librdkafka to errors it
# raises itself and reads False for an error constructed from a code -
# including for _ALL_BROKERS_DOWN, which is as retriable as an error gets.
PERMANENT_ERROR_CODES = frozenset(
    {
        # The broker's message.max.bytes says no, and will say no again.
        KafkaError.MSG_SIZE_TOO_LARGE,
        # Checksum or format failure. The same bytes stay malformed.
        KafkaError.INVALID_MSG,
        KafkaError.RECORD_LIST_TOO_LARGE,
        # librdkafka could not turn our value or key into bytes. We build
        # both ourselves below, so this is a payload we can never send.
        KafkaError._VALUE_SERIALIZATION,
        KafkaError._KEY_SERIALIZATION,
    }
)


class KafkaPublisher:
    """A `Publisher` that delivers to Kafka. See services/publisher.py.

    Holds one long-lived `Producer`. That is not an optimisation so much as a
    requirement: a producer owns a background thread, a connection pool and
    the in-flight batching that makes `enable.idempotence` work. Building one
    per batch would reconnect every few seconds and lose the sequence numbers
    that keep messages in order.

    Satisfies the `Publisher` protocol without inheriting from it - the
    protocol is a shape, not a base class - so this file does not import the
    dispatcher's world and the dispatcher does not import Kafka's.
    """

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        message_timeout_ms: int,
    ) -> None:
        self._producer = Producer(
            {
                "bootstrap.servers": bootstrap_servers,
                # The setting that matters most here, and the least obvious.
                #
                # librdkafka retries internally on a failed send. Without
                # idempotence those retries can land out of order within a
                # partition - message 2 arriving before message 1 because
                # message 1 was retried. That would silently undo the
                # ordering guard the dispatcher builds with ordering_key:
                # we would hold the enrollment back correctly, hand both to
                # Kafka in the right order, and have Kafka reorder them.
                #
                # Turning it on also pins acks=all, retries high, and
                # max.in.flight<=5, which is why those are not set again
                # below except where the explicitness is worth it.
                "enable.idempotence": True,
                # Every in-sync replica has the message before it counts as
                # delivered. Implied by idempotence; written out because
                # `published_at` is a promise and this is the setting that
                # decides what it promises.
                "acks": "all",
                # How long one message may keep being retried inside
                # librdkafka before it is reported failed. This is the clock
                # behind every _MSG_TIMED_OUT, and it bounds how long a
                # dispatcher pass can hold its row locks.
                "message.timeout.ms": message_timeout_ms,
            }
        )
        # Flush has to outlast the per-message timeout, or we would give up
        # waiting while librdkafka is still legitimately retrying and report
        # events as failed that were about to succeed. The margin covers the
        # final delivery callbacks.
        self._flush_timeout_seconds = (message_timeout_ms / 1000) + 5

    async def publish(
        self, events: Sequence[OutboxEvent]
    ) -> dict[UUID, DeliveryError | None]:
        """Send a batch, and report on each event separately.

        Three phases, and the middle one is where the speed comes from:

            1. serialise and queue every message  (no network)
            2. one flush                          (the network)
            3. collect per-message verdicts

        `produce()` only appends to librdkafka's internal queue and returns
        immediately, so a hundred messages cost roughly one round trip rather
        than a hundred.
        """
        results: dict[UUID, DeliveryError | None] = {}

        for event in events:
            try:
                value = json.dumps(event.payload).encode("utf-8")
            except (TypeError, ValueError) as err:
                # The payload will not become JSON. It will not become JSON
                # in thirty minutes either, so there is nothing to retry.
                results[event.id] = PermanentDeliveryError(
                    f"payload is not serialisable: {err}"
                )
                continue

            try:
                self._producer.produce(
                    topic=event.topic,
                    key=(
                        event.partition_key.encode("utf-8")
                        if event.partition_key is not None
                        else None
                    ),
                    value=value,
                    on_delivery=self._record_verdict(results, event.id),
                )
            except BufferError:
                # The local queue is full - we are producing faster than the
                # broker accepts. Nothing is wrong with this message.
                results[event.id] = TransientDeliveryError("the producer queue is full")
            except KafkaException as err:
                results[event.id] = self._classify(err.args[0])

        # flush() blocks, and blocking the event loop would stall the
        # listener connection and the ticker along with it. A thread is the
        # right tool: librdkafka does the waiting in C, with the GIL
        # released, so the loop keeps running.
        #
        # The return value is how many messages never got a verdict.
        unresolved = await asyncio.to_thread(
            self._producer.flush, self._flush_timeout_seconds
        )
        if unresolved:
            logger.warning(
                "kafka flush left %d message(s) unresolved after %.0fs",
                unresolved,
                self._flush_timeout_seconds,
            )

        # Anything with no verdict is still sitting in librdkafka's queue. It
        # may yet be delivered, which is exactly why this is transient and
        # not permanent - and why consumers have to tolerate a duplicate.
        # This is where at-least-once becomes a real possibility rather than
        # a theoretical one.
        for event in events:
            results.setdefault(
                event.id,
                TransientDeliveryError("no delivery report before flush timeout"),
            )

        return results

    @staticmethod
    def _record_verdict(results: dict[UUID, DeliveryError | None], event_id: UUID):
        """Build the per-message callback librdkafka calls on delivery.

        A factory rather than one closure written in the loop, because a
        closure defined in a loop captures the *variable*, not its value -
        every callback would report against the last event in the batch.

        These callbacks run on librdkafka's own thread while `flush` is
        waiting, so `results` is written from a thread that is not the event
        loop's. Safe here for a narrow reason: a dict assignment is atomic
        under the GIL, and nothing reads the dict until `flush` has returned
        and every callback has run.
        """

        def on_delivery(err: KafkaError | None, _msg: object) -> None:
            results[event_id] = None if err is None else KafkaPublisher._classify(err)

        return on_delivery

    @staticmethod
    def _classify(err: KafkaError) -> DeliveryError:
        """Turn one Kafka error into one of our two words.

        The whole of this file's reason for existing is these six lines.
        """
        message = f"{err.name()}: {err.str()}"
        if err.code() in PERMANENT_ERROR_CODES:
            return PermanentDeliveryError(message)
        return TransientDeliveryError(message)

    def close(self) -> None:
        """Flush whatever is still queued, then stop.

        Deliberately not on the `Publisher` protocol. The dispatcher holds a
        publisher and should not know whether the thing it holds needs
        shutting down; the entry point constructs the concrete publisher and
        is the right place to close it.

        Called on SIGTERM. Skipping it is survivable - unflushed rows were
        never marked published, so they are simply sent again - but it turns
        a tidy redeploy into a round of duplicates for no reason.
        """
        self._producer.flush(self._flush_timeout_seconds)
