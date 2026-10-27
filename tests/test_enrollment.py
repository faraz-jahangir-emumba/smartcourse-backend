"""Enrollment. FR-06 to FR-10, UC-02.

Most of these go through the API, like the rest of the suite. The last one
cannot: proving the capacity lock works needs two real transactions running
at the same time, and the client fixture deliberately gives every request the
same rolled-back session. See the comment on that test.
"""

import asyncio
from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smartcourse.domain.errors import ConflictError
from smartcourse.infra.db.models.course import Course, CoursePrerequisite
from smartcourse.infra.db.models.enrollment import Enrollment
from smartcourse.infra.db.models.user import User
from smartcourse.infra.db.repositories import CourseRepository, EnrollmentRepository
from smartcourse.services import enrollment as enrollment_service
from tests.conftest import auth_header, register


async def _make_course(
    client: AsyncClient,
    headers: dict,
    title: str = "Python",
    capacity: int | None = None,
) -> str:
    body: dict = {"title": title, "description": "x"}
    if capacity is not None:
        body["capacity"] = capacity
    response = await client.post("/courses", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _publish(session: AsyncSession, course_id: str) -> None:
    """Mark a course ready. No publish endpoint yet - that is the second half
    of Module 2."""
    course = await session.get(Course, UUID(course_id))
    course.status = "ready"
    await session.flush()


async def _ready_course(
    client: AsyncClient, session: AsyncSession, headers: dict, **kwargs
) -> str:
    course_id = await _make_course(client, headers, **kwargs)
    await _publish(session, course_id)
    return course_id


# --- the happy path --------------------------------------------------------


async def test_a_student_can_enrol(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    course_id = await _ready_course(client, session, instructor)

    response = await client.post(
        f"/courses/{course_id}/enrollments/enroll", headers=student
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["course_id"] == course_id
    assert body["status"] == "active"
    # FR-09's progress record is this row. completed_at stays null until the
    # course is finished, which nothing can do until Module 3.
    assert body["completed_at"] is None


# --- FR-06, duplicates -----------------------------------------------------


async def test_enrolling_twice_is_refused(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """FR-06."""
    course_id = await _ready_course(client, session, instructor)
    await client.post(f"/courses/{course_id}/enrollments/enroll", headers=student)

    response = await client.post(
        f"/courses/{course_id}/enrollments/enroll", headers=student
    )

    assert response.status_code == 409
    assert "already enrolled" in response.json()["error"]["message"].lower()


# --- visibility ------------------------------------------------------------


async def test_cannot_enrol_in_a_draft(
    client: AsyncClient, instructor: dict, student: dict
) -> None:
    """404, not 403.

    A student cannot see drafts, so a 403 here would confirm one exists -
    turning this endpoint into a way to discover unpublished work. Same rule
    the read endpoints follow.
    """
    course_id = await _make_course(client, instructor)

    response = await client.post(
        f"/courses/{course_id}/enrollments/enroll", headers=student
    )

    assert response.status_code == 404


async def test_enrolling_in_a_course_that_does_not_exist_is_404(
    client: AsyncClient, student: dict
) -> None:
    response = await client.post(
        f"/courses/{uuid4()}/enrollments/enroll", headers=student
    )

    assert response.status_code == 404


# --- the student role ------------------------------------------------------


async def test_enrolling_requires_the_student_role(
    client: AsyncClient, instructor: dict, session: AsyncSession
) -> None:
    """UC-07 only means something if the role is what grants this."""
    course_id = await _ready_course(client, session, instructor)
    await register(client, "teacher@example.com", ["instructor"])
    headers = await auth_header(client, "teacher@example.com")

    response = await client.post(
        f"/courses/{course_id}/enrollments/enroll", headers=headers
    )

    assert response.status_code == 403
    assert "student role" in response.json()["error"]["message"]


async def test_an_instructor_can_enrol_in_their_own_course(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Allowed. Nothing in the brief forbids it, there is no harm, and
    blocking it would mean a special case plus a confusing error for somebody
    testing their own material."""
    await register(client, "both@example.com", ["instructor", "student"])
    headers = await auth_header(client, "both@example.com")
    course_id = await _ready_course(client, session, headers)

    response = await client.post(
        f"/courses/{course_id}/enrollments/enroll", headers=headers
    )

    assert response.status_code == 201


# --- FR-07, capacity -------------------------------------------------------


async def test_a_full_course_is_refused(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """FR-07, one request at a time. The concurrent case is at the bottom."""
    course_id = await _ready_course(client, session, instructor, capacity=1)
    first = await client.post(
        f"/courses/{course_id}/enrollments/enroll", headers=student
    )
    assert first.status_code == 201

    await register(client, "second@example.com", ["student"])
    other = await auth_header(client, "second@example.com")
    response = await client.post(
        f"/courses/{course_id}/enrollments/enroll", headers=other
    )

    assert response.status_code == 409
    assert response.json()["error"]["details"]["capacity"] == 1


async def test_withdrawing_frees_a_seat(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """Only active enrollments occupy a seat. Counting history instead would
    fill a course permanently."""
    course_id = await _ready_course(client, session, instructor, capacity=1)
    await client.post(f"/courses/{course_id}/enrollments/enroll", headers=student)
    await client.post(f"/courses/{course_id}/enrollments/withdraw", headers=student)

    await register(client, "second@example.com", ["student"])
    other = await auth_header(client, "second@example.com")
    response = await client.post(
        f"/courses/{course_id}/enrollments/enroll", headers=other
    )

    assert response.status_code == 201


async def test_null_capacity_means_unlimited(
    client: AsyncClient, instructor: dict, session: AsyncSession
) -> None:
    """Null and zero are different things, which is why capacity is nullable
    rather than defaulting to 0."""
    course_id = await _ready_course(client, session, instructor)

    for i in range(3):
        await register(client, f"many{i}@example.com", ["student"])
        headers = await auth_header(client, f"many{i}@example.com")
        response = await client.post(
            f"/courses/{course_id}/enrollments/enroll", headers=headers
        )
        assert response.status_code == 201


# --- FR-08, prerequisites --------------------------------------------------


async def test_an_unmet_prerequisite_is_refused(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """FR-08, and the error names what is missing - a UUID is not something a
    student can act on."""
    basics = await _ready_course(client, session, instructor, title="Python Basics")
    advanced = await _ready_course(client, session, instructor, title="Advanced")
    session.add(
        CoursePrerequisite(
            course_id=UUID(advanced), prerequisite_course_id=UUID(basics)
        )
    )
    await session.flush()

    response = await client.post(
        f"/courses/{advanced}/enrollments/enroll", headers=student
    )

    assert response.status_code == 409
    assert response.json()["error"]["details"]["missing"] == ["Python Basics"]


async def test_an_active_prerequisite_does_not_count(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """Being part way through Python Basics is not knowing Python. Only a
    completed enrollment satisfies a prerequisite."""
    basics = await _ready_course(client, session, instructor, title="Python Basics")
    advanced = await _ready_course(client, session, instructor, title="Advanced")
    session.add(
        CoursePrerequisite(
            course_id=UUID(advanced), prerequisite_course_id=UUID(basics)
        )
    )
    await session.flush()
    await client.post(f"/courses/{basics}/enrollments/enroll", headers=student)

    response = await client.post(
        f"/courses/{advanced}/enrollments/enroll", headers=student
    )

    assert response.status_code == 409


async def test_a_completed_prerequisite_lets_you_in(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """Set up by hand, because nothing in Module 2 can complete an enrollment
    - that is FR-14/15, which the Guidelines put in Week 3. The rule is
    correct; the state that satisfies it arrives later."""
    basics = await _ready_course(client, session, instructor, title="Python Basics")
    advanced = await _ready_course(client, session, instructor, title="Advanced")
    session.add(
        CoursePrerequisite(
            course_id=UUID(advanced), prerequisite_course_id=UUID(basics)
        )
    )
    await session.flush()

    await client.post(f"/courses/{basics}/enrollments/enroll", headers=student)
    enrollment = await session.scalar(
        select(Enrollment).where(Enrollment.course_id == UUID(basics))
    )
    enrollment.status = "completed"
    enrollment.completed_at = enrollment.enrolled_at
    await session.flush()

    response = await client.post(
        f"/courses/{advanced}/enrollments/enroll", headers=student
    )

    assert response.status_code == 201


# --- FR-10, withdrawal and history -----------------------------------------


async def test_withdrawing_keeps_the_row(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """FR-10. The status changes; nothing is deleted."""
    course_id = await _ready_course(client, session, instructor)
    await client.post(f"/courses/{course_id}/enrollments/enroll", headers=student)

    response = await client.post(
        f"/courses/{course_id}/enrollments/withdraw", headers=student
    )

    assert response.status_code == 200
    assert response.json()["status"] == "withdrawn"


async def test_withdrawing_when_not_enrolled_is_404(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    course_id = await _ready_course(client, session, instructor)

    response = await client.post(
        f"/courses/{course_id}/enrollments/withdraw", headers=student
    )

    assert response.status_code == 404


async def test_you_can_enrol_again_after_withdrawing(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """The reason uq_enrollments_one_active is a *partial* index: one active
    enrollment, unlimited history."""
    course_id = await _ready_course(client, session, instructor)
    await client.post(f"/courses/{course_id}/enrollments/enroll", headers=student)
    await client.post(f"/courses/{course_id}/enrollments/withdraw", headers=student)

    response = await client.post(
        f"/courses/{course_id}/enrollments/enroll", headers=student
    )
    assert response.status_code == 201

    history = await client.get("/users/me/enrollments", headers=student)
    assert history.json()["total"] == 2


async def test_my_enrollments_includes_the_ones_i_left(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """Filtering history out here would make FR-10 pointless."""
    course_id = await _ready_course(client, session, instructor)
    await client.post(f"/courses/{course_id}/enrollments/enroll", headers=student)
    await client.post(f"/courses/{course_id}/enrollments/withdraw", headers=student)

    response = await client.get("/users/me/enrollments", headers=student)

    assert response.json()["total"] == 1
    assert response.json()["items"][0]["status"] == "withdrawn"


# --- the roster ------------------------------------------------------------


async def test_the_owning_instructor_sees_the_roster(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    course_id = await _ready_course(client, session, instructor)
    await client.post(f"/courses/{course_id}/enrollments/enroll", headers=student)

    response = await client.get(
        f"/courses/{course_id}/enrollments", headers=instructor
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1


async def test_a_student_cannot_see_the_roster(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """A class list is personal data, and who else is on a course is other
    people's business. 404 rather than 403, matching the course read rules."""
    course_id = await _ready_course(client, session, instructor)
    await client.post(f"/courses/{course_id}/enrollments/enroll", headers=student)

    response = await client.get(f"/courses/{course_id}/enrollments", headers=student)

    assert response.status_code == 404


async def test_another_instructor_cannot_see_the_roster(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    course_id = await _ready_course(client, session, instructor)
    await client.post(f"/courses/{course_id}/enrollments/enroll", headers=student)
    await register(client, "other@example.com", ["instructor"])
    other = await auth_header(client, "other@example.com")

    response = await client.get(f"/courses/{course_id}/enrollments", headers=other)

    assert response.status_code == 404


# --- the one that needed a different approach ------------------------------


async def test_two_students_cannot_take_the_last_seat(engine) -> None:
    """FR-07 under genuine concurrency. The test the rest of the suite cannot
    express.

    Every other test here shares one session, wrapped in a transaction that
    is rolled back afterwards. That is what keeps them independent - and it
    makes a race impossible to stage, because two "concurrent" requests would
    be the same transaction and would see each other's uncommitted work.

    So this one steps outside the fixtures: real sessions, real transactions,
    real commits, cleaned up by hand at the end.

    Without FOR UPDATE in lock_course, both coroutines count zero against a
    capacity of one, both find room, and the course ends up with two active
    enrollments. The assertion below is the one that would fail.
    """
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]

    # Committed on purpose. Two separate transactions have to be able to see
    # this, which rules out the usual rolled-back fixture.
    async with maker() as setup:
        teacher = User(
            email=f"race-teacher-{tag}@example.com",
            password="x",
            full_name="Teacher",
            roles=["instructor"],
        )
        first = User(
            email=f"race-a-{tag}@example.com",
            password="x",
            full_name="A",
            roles=["student"],
        )
        second = User(
            email=f"race-b-{tag}@example.com",
            password="x",
            full_name="B",
            roles=["student"],
        )
        setup.add_all([teacher, first, second])
        await setup.flush()

        course = Course(
            instructor_id=teacher.id,
            title=f"One Seat {tag}",
            status="ready",
            capacity=1,
        )
        setup.add(course)
        await setup.commit()

        course_id = course.id
        user_ids = [first.id, second.id]

    async def attempt(user_id: UUID) -> str:
        async with maker() as s:
            user = await s.get(User, user_id)
            try:
                await enrollment_service.enroll(
                    EnrollmentRepository(s),
                    CourseRepository(s),
                    course_id=course_id,
                    user=user,
                )
                await s.commit()
                return "enrolled"
            except ConflictError:
                await s.rollback()
                return "refused"

    try:
        results = await asyncio.gather(*(attempt(uid) for uid in user_ids))

        assert sorted(results) == ["enrolled", "refused"]

        # The assertion that actually matters. The one above could pass by
        # luck of scheduling; this one is the requirement itself.
        async with maker() as check:
            active = await check.scalars(
                select(Enrollment).where(
                    Enrollment.course_id == course_id,
                    Enrollment.status == "active",
                )
            )
            assert len(list(active)) == 1
    finally:
        async with maker() as cleanup:
            for enrollment in await cleanup.scalars(
                select(Enrollment).where(Enrollment.course_id == course_id)
            ):
                await cleanup.delete(enrollment)
            await cleanup.flush()
            await cleanup.delete(await cleanup.get(Course, course_id))
            for uid in [*user_ids, teacher.id]:
                await cleanup.delete(await cleanup.get(User, uid))
            await cleanup.commit()


# --- how full is it, FR-07 -------------------------------------------------


async def test_a_course_reports_how_many_are_enrolled(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """`capacity` is the ceiling the instructor set and never moves;
    `enrolled_count` is what changes.

    Without the count there is no way to tell a course with two free places
    from a full one, short of trying to enrol and reading the 409 - which
    is a poor way for a catalogue to show "1 place left".
    """
    course_id = await _ready_course(client, session, instructor, capacity=2)

    before = await client.get(f"/courses/{course_id}", headers=student)
    assert before.json()["capacity"] == 2
    assert before.json()["enrolled_count"] == 0

    await client.post(f"/courses/{course_id}/enrollments/enroll", headers=student)

    after = await client.get(f"/courses/{course_id}", headers=student)
    assert after.json()["capacity"] == 2, "the ceiling does not move"
    assert after.json()["enrolled_count"] == 1


async def test_withdrawing_frees_the_count_too(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """Only active enrolments are counted - the same rule the capacity check
    uses, so the number a catalogue shows and the number that refuses a
    student cannot disagree."""
    course_id = await _ready_course(client, session, instructor, capacity=2)
    await client.post(f"/courses/{course_id}/enrollments/enroll", headers=student)
    await client.post(f"/courses/{course_id}/enrollments/withdraw", headers=student)

    body = (await client.get(f"/courses/{course_id}", headers=student)).json()

    assert body["enrolled_count"] == 0


async def test_the_listing_reports_it_too(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """A correlated subquery, so twenty courses is still one round trip
    rather than twenty counts."""
    course_id = await _ready_course(client, session, instructor, capacity=5)
    await client.post(f"/courses/{course_id}/enrollments/enroll", headers=student)

    listing = (await client.get("/courses", headers=student)).json()

    found = next(c for c in listing["items"] if c["id"] == course_id)
    assert found["enrolled_count"] == 1


async def test_a_freshly_created_course_reports_zero(
    client: AsyncClient, instructor: dict
) -> None:
    """The case that broke first.

    enrolled_count is a column_property - a subquery added to every SELECT.
    A course that was just INSERTed was never SELECTed, so the attribute has
    no value, and reading it makes SQLAlchemy fetch it lazily. Under asyncio
    that is IO where IO cannot happen, and it fails with MissingGreenlet -
    an error mentioning neither courses nor counts.

    CourseRepository.add refreshes after the insert for exactly this.
    """
    response = await client.post(
        "/courses", json={"title": "Brand new"}, headers=instructor
    )

    assert response.status_code == 201
    assert response.json()["enrolled_count"] == 0


async def test_completing_frees_a_seat_and_lowers_the_count(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """Capacity is how many can study at once, not how many places exist
    for all time.

    A course with capacity 2 whose two students finish would otherwise be
    permanently full and could never be taught again - the course is a
    reusable asset, not a one-off cohort.

    Set directly because nothing can complete an enrollment yet: that is
    FR-14/15, which the Guidelines put in Week 3. The test exists now
    precisely because it is unreachable - whoever writes the completion
    code in Module 3 will not stop to ask what completing should do to
    capacity, and this says so.
    """
    course_id = await _ready_course(client, session, instructor, capacity=1)
    await client.post(f"/courses/{course_id}/enrollments/enroll", headers=student)

    full = (await client.get(f"/courses/{course_id}", headers=student)).json()
    assert full["enrolled_count"] == 1

    await register(client, "second@example.com", ["student"])
    other = await auth_header(client, "second@example.com")
    assert (
        await client.post(
            f"/courses/{course_id}/enrollments/enroll", headers=other
        )
    ).status_code == 409

    enrollment = await session.scalar(
        select(Enrollment).where(Enrollment.course_id == UUID(course_id))
    )
    enrollment.status = "completed"
    enrollment.completed_at = enrollment.enrolled_at
    await session.flush()

    after = (await client.get(f"/courses/{course_id}", headers=other)).json()
    assert after["enrolled_count"] == 0, "a finished student is not taking a seat"

    assert (
        await client.post(
            f"/courses/{course_id}/enrollments/enroll", headers=other
        )
    ).status_code == 201


async def test_the_count_is_what_a_client_compares_against_capacity(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """The whole reason it is active-only.

    A client decides whether to disable its Enrol button with
    `enrolled_count === capacity` and nothing else. If completed students
    were counted, that comparison would say "full" about a course with free
    places, and the button would be wrong rather than merely stale.
    """
    course_id = await _ready_course(client, session, instructor, capacity=1)

    body = (await client.get(f"/courses/{course_id}", headers=student)).json()
    assert body["enrolled_count"] < body["capacity"], "a client would enable it"

    await client.post(f"/courses/{course_id}/enrollments/enroll", headers=student)

    body = (await client.get(f"/courses/{course_id}", headers=student)).json()
    assert body["enrolled_count"] == body["capacity"], "a client would disable it"
