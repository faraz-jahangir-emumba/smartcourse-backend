"""Timing properties of password handling.

Two of the review findings are about *how long* things take rather than what
they return, so these tests measure. Both use deliberately generous margins:
the real differences are large - roughly 50x and 4x - so a threshold near the
middle catches a regression without failing on a busy machine.
"""

import asyncio
import time

from httpx import AsyncClient

from smartcourse.infra.security import hash_password
from tests.conftest import PASSWORD, register


async def _median(run, times: int = 3) -> float:
    samples = []
    for _ in range(times):
        start = time.perf_counter()
        await run()
        samples.append(time.perf_counter() - start)
    return sorted(samples)[len(samples) // 2]


async def test_an_unknown_email_takes_as_long_as_a_wrong_password(
    client: AsyncClient,
) -> None:
    """Identical error messages are not enough on their own.

    Before the fix, an unknown email returned as soon as the database said
    "no row" - about a millisecond - while a wrong password waited for
    Argon2, around fifty. That gap is easy to measure from outside, so the
    login form still answered "is this address registered?", just in time
    rather than in words.

    The fix verifies against a dummy hash before refusing, so both paths do
    the same work.
    """
    await register(client, "real@example.com")

    async def unknown_email() -> None:
        await client.post(
            "/auth/login",
            json={"email": "nobody@example.com", "password": PASSWORD},
        )

    async def wrong_password() -> None:
        await client.post(
            "/auth/login",
            json={"email": "real@example.com", "password": "not-the-password"},
        )

    unknown = await _median(unknown_email)
    wrong = await _median(wrong_password)

    # Before the fix this ratio was around 0.02. After it, close to 1.0.
    # Half is comfortably between the two and nowhere near either.
    assert unknown > wrong * 0.5, (
        f"unknown email took {unknown:.3f}s but a wrong password took "
        f"{wrong:.3f}s - the gap leaks which addresses are registered"
    )


async def test_hashing_does_not_block_the_event_loop() -> None:
    """The loop must stay free to serve other requests while a hash runs.

    Argon2 is deliberately slow and memory-hard - that is the point of it -
    but it is ordinary blocking CPU work, not waiting. Called straight from
    an `async def` it holds the event loop for its whole duration, so a burst
    of logins stalls every other request on that worker, including ones that
    never touch a password. asyncio.to_thread moves it off the loop.

    Measured with a ticker: a coroutine that wakes every 5ms and counts. If
    the loop is blocked it never gets to run and the count stays at zero. If
    it is free, a hash of roughly 40-60ms gives it several turns.

    An earlier version of this test timed four hashes against one and
    asserted they overlapped. That passed locally and failed in CI at 4.07x,
    and CI was right: argon2-cffi defaults to parallelism=4, so one hash
    already saturates a two-core runner and no amount of threading will make
    four of them finish sooner. Overlap depends on spare cores. Keeping the
    loop free does not, and is the property the fix is actually for.
    """
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    counting = asyncio.create_task(ticker())
    try:
        # Let the ticker reach its first await before the hash starts.
        await asyncio.sleep(0)
        await hash_password("one password")
    finally:
        counting.cancel()

    assert ticks >= 3, (
        f"the loop ticked {ticks} times during a hash - it is blocked, so "
        f"every other request waits for Argon2 to finish"
    )
