"""The request's transaction boundary.

Every other test in this suite overrides get_session so its work can be
rolled back. That is what keeps them independent - and it means the real
get_session, the one that actually commits, is never exercised by them.

This file is the exception. It uses the real dependency, on purpose, because
the thing being checked is *when* the commit happens relative to the
response.
"""

from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from smartcourse.api.deps import SessionDep
from smartcourse.api.errors import register_exception_handlers
from smartcourse.infra.db.models.course import Course, Module
from smartcourse.infra.db.models.user import User


async def test_a_failed_commit_is_not_reported_as_success(engine) -> None:
    """A commit that fails must not arrive after a 201 has gone out.

    A dependency with yield ends either before or after the response is sent.
    FastAPI's default is after, which means get_session's commit runs once
    the client already holds its 201. If the commit then fails, the caller
    has been told the work succeeded and nothing can take it back.

    That is not hypothetical. uq_modules_course_position is DEFERRABLE
    INITIALLY DEFERRED, so two modules sharing a position pass every flush
    and are only rejected at COMMIT - exactly the moment that was happening
    too late.

    SessionDep declares scope="function", so the commit runs after the
    endpoint returns but *before* the response is sent. The failure becomes a
    500 the caller can act on.

    Nothing is cleaned up afterwards because nothing is kept: the failed
    commit rolls the whole transaction back, including the user and course
    created on the way.
    """
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/deferred-boom", status_code=201)
    async def deferred_boom(session: SessionDep) -> dict[str, bool]:
        instructor = User(
            email=f"boom-{uuid4().hex[:8]}@example.com",
            password="x",
            full_name="Boom",
            roles=["instructor"],
        )
        session.add(instructor)
        await session.flush()

        course = Course(
            instructor_id=instructor.id, title="Deferred", status="draft"
        )
        session.add(course)
        await session.flush()

        session.add_all(
            [
                Module(course_id=course.id, title="First", position=1),
                Module(course_id=course.id, title="Second", position=1),
            ]
        )
        # Passes. The constraint is deferred, so duplicates are legal until
        # the transaction ends - which is the whole point of deferring it.
        await session.flush()

        return {"ok": True}

    # raise_app_exceptions=False so a failure becomes a real HTTP response,
    # the way it would reach a browser, rather than being re-raised into the
    # test.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/deferred-boom")

    # 409 rather than 500 because of the IntegrityError handler added for
    # review item #2: a violated unique constraint is now reported as a
    # conflict. Which code it is matters less than the line below it.
    assert response.status_code == 409

    # This is the assertion the test exists for. Before scope="function" it
    # was 201: the endpoint succeeded, the response went out, and only then
    # did the commit fail, with nobody left to tell.
    assert response.status_code != 201
