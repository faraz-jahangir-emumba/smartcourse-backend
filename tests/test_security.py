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
    """Argon2 is deliberately slow and memory-hard - that is the point of it -
    but it is ordinary blocking CPU work. Called straight from an `async def`
    it holds the event loop for its whole duration, so a burst of logins
    stalls every other request on that worker, including ones that never
    touch a password.

    asyncio.to_thread moves it off the loop. argon2-cffi releases the GIL
    while its C code runs, so the hashes genuinely overlap.

    Four at once: serialised that is about 4x one hash, overlapped it is
    close to 1x. The threshold is 3x - far enough from both that thread
    scheduling noise cannot reach it.
    """
    start = time.perf_counter()
    await hash_password("one password")
    single = time.perf_counter() - start

    start = time.perf_counter()
    await asyncio.gather(*(hash_password(f"password {i}") for i in range(4)))
    concurrent = time.perf_counter() - start

    assert concurrent < single * 3, (
        f"four hashes took {concurrent:.3f}s against {single:.3f}s for one - "
        f"they are running one after another, so the loop is blocked"
    )
