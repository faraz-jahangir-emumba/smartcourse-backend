"""The outbox dispatcher. FR-16, FR-17, FR-18.

Runs beside uvicorn and the Temporal worker, in its own process:

    uv run python -m smartcourse.dispatcher

This is the only file that knows all three of the pieces involved: that the
destination is Kafka, that waking up means a Postgres LISTEN, and that
shutting down means a signal. The loop in services/dispatcher.py knows none
of them - it is handed a `Publisher`, an `asyncio.Event` to wake on, and
another to stop on.

Exactly one instance. Two would each lock different rows and could publish
two events for one enrollment out of order, which is the single thing the
ordering guard exists to prevent. The cost is a single point of failure, and
it is a cheap one: a dispatcher that dies has marked nothing it did not do,
so a restart simply resumes.
"""

import asyncio
import contextlib
import logging
import signal

import asyncpg

from smartcourse.config import get_settings
from smartcourse.infra.db.session import SessionFactory, dispose_engine
from smartcourse.infra.kafka import KafkaPublisher
from smartcourse.services import dispatcher

logger = logging.getLogger(__name__)

logging.basicConfig(level=logging.INFO)

# The channel the outbox trigger notifies on. Must match the migration that
# created `outbox_notify()`; there is no way for one to tell the other, so
# changing it means changing both and a notification silently going nowhere
# if you change only one.
CHANNEL = "outbox_new"

# How long to wait before rebuilding a dropped LISTEN connection. Short,
# because while it is down the only thing waking the dispatcher is the
# five-minute tick - correct, but five minutes of latency on every event.
RECONNECT_SECONDS = 5.0

# Which mechanism a FailedEvent row is attributed to. The loop is written not
# to know what it publishes to, so the name is supplied here, where the
# concrete publisher is built. Constrained by a CHECK to FAILURE_SOURCES, so
# a wrong value fails at the database rather than skewing a dashboard.
FAILURE_SOURCE = "kafka"


async def _listen(dsn: str, wake: asyncio.Event, stop: asyncio.Event) -> None:
    """Hold a connection open and set `wake` whenever an event is inserted.

    A raw asyncpg connection, not one from the SQLAlchemy pool. LISTEN is a
    property of a *connection*: a pooled one is handed back after each use
    and the subscription dies with it, silently, leaving a dispatcher that
    looks fine and only ever wakes on the tick.

    Reconnects on its own, and sets `wake` after each reconnection. Anything
    inserted while the connection was down raised a notification nobody was
    listening for - those are not queued - so the first thing to do on
    coming back is to assume there is work.
    """
    while not stop.is_set():
        conn = None
        try:
            conn = await asyncpg.connect(dsn)
            await conn.add_listener(CHANNEL, lambda *_: wake.set())
            logger.info("listening on %s", CHANNEL)

            # Anything that arrived while we were not listening is
            # unrecoverable as a notification, so start by draining.
            wake.set()

            # Hold the connection open. Nothing to do here but notice if it
            # dies - the callback above does the real work, on asyncpg's own
            # task.
            while not stop.is_set() and not conn.is_closed():
                # The timeout is the point, not an error: it is how often
                # this checks whether the connection is still alive.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=RECONNECT_SECONDS)

            if conn.is_closed():
                logger.warning("listener connection dropped; reconnecting")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Deliberately broad. This task existing is what keeps latency
            # low, and any exception that escapes it would leave the
            # dispatcher quietly polling every five minutes instead. Better
            # to log and try again.
            logger.exception("listener failed; retrying in %.0fs", RECONNECT_SECONDS)
            await asyncio.sleep(RECONNECT_SECONDS)
        finally:
            if conn is not None and not conn.is_closed():
                await conn.close()


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Ask the loop to finish its current pass and exit.

    SIGTERM is what Docker sends on `compose down` or a redeploy, and SIGINT
    is Ctrl-C. Both set the same flag; `run_forever` checks it between
    passes, which is the only safe moment - a pass is one transaction, and
    interrupting it mid-flight would mean messages sent and never recorded.

    `add_signal_handler` is the right way to do this and is not implemented
    on Windows, where the fallback is the older `signal.signal`. Only SIGINT
    is really delivered there, which is enough: the SIGTERM case is Docker,
    and Docker runs Linux.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop.set())


async def main() -> None:
    settings = get_settings()

    publisher = KafkaPublisher(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        message_timeout_ms=settings.kafka_message_timeout_ms,
    )

    wake = asyncio.Event()
    stop = asyncio.Event()
    _install_signal_handlers(stop)

    # asyncpg speaks the plain URL. The `+asyncpg` suffix is SQLAlchemy's way
    # of naming a driver and means nothing outside it.
    listener = asyncio.create_task(
        _listen(settings.database_url.replace("+asyncpg", ""), wake, stop)
    )

    try:
        await dispatcher.run_forever(
            SessionFactory,
            publisher,
            wake=wake,
            stop=stop,
            batch_size=settings.dispatcher_batch_size,
            tick_seconds=settings.dispatcher_tick_seconds,
            warn_after_hours=settings.dispatcher_warn_after_hours,
            abandon_after_hours=settings.dispatcher_abandon_after_hours,
            failure_source=FAILURE_SOURCE,
        )
    finally:
        listener.cancel()
        # Flush whatever librdkafka still holds. Skipping it is survivable -
        # unflushed rows were never marked published, so they go again - but
        # it turns a tidy redeploy into a round of duplicates for nothing.
        publisher.close()
        # Hand the pooled connections back rather than leaving Postgres to
        # notice the process is gone.
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
