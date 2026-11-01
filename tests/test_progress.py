"""Lesson progress and course completion. FR-14, FR-15, UC-03.

Through the API, like most of the suite. Two of these reach into the
session directly - the ones that have to publish a course, because there is
no "unpublish" and driving a real Temporal workflow for a progress test
would be testing the wrong thing.

The one worth reading first is
test_completing_a_course_does_not_lock_the_student_out. FR-15 would have
broken content access the day it shipped, and nothing in Module 2 could
have caught it: `completed` was unreachable, so every test of the enrolment
gate ran against the only status that existed.
"""

from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse.infra.db.models.course import Course
from smartcourse.infra.db.models.enrollment import Enrollment
from tests.conftest import auth_header, register

CONTENT = "The material that proves the gate still lets them in."


async def _course_with_lessons(
    client: AsyncClient,
    session: AsyncSession,
    headers: dict,
    *,
    lessons: int = 2,
    capacity: int | None = None,
) -> tuple[str, list[str]]:
    """A published course with `lessons` lessons. Returns (course_id, ids)."""
    body: dict = {"title": f"Course {uuid4().hex[:6]}", "description": "x"}
    if capacity is not None:
        body["capacity"] = capacity
    course_id = (await client.post("/courses", json=body, headers=headers)).json()["id"]

    module_id = (
        await client.post(
            f"/courses/{course_id}/modules", json={"title": "M"}, headers=headers
        )
    ).json()["id"]

    lesson_ids = [
        (
            await client.post(
                f"/modules/{module_id}/lessons",
                json={"title": f"Lesson {i}", "content": CONTENT},
                headers=headers,
            )
        ).json()["id"]
        for i in range(lessons)
    ]

    course = await session.get(Course, UUID(course_id))
    course.status = "ready"
    await session.flush()
    return course_id, lesson_ids


async def _enrolled(
    client: AsyncClient, session: AsyncSession, instructor: dict, student: dict, **kw
) -> tuple[str, list[str]]:
    course_id, lesson_ids = await _course_with_lessons(
        client, session, instructor, **kw
    )
    response = await client.post(
        f"/courses/{course_id}/enrollments/enroll", headers=student
    )
    assert response.status_code == 201, response.text
    return course_id, lesson_ids


# --- marking a lesson finished ---------------------------------------------


async def test_a_student_can_complete_a_lesson(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """200 with the whole progress, so a client can redraw its bar without
    a second call."""
    _, lesson_ids = await _enrolled(client, session, instructor, student, lessons=2)

    response = await client.post(
        f"/lessons/{lesson_ids[0]}/completion", headers=student
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["completed"] == 1
    assert body["total"] == 2
    assert body["percent"] == 50
    assert body["newly_completed"] is True
    assert body["course_completed"] is False
    assert body["status"] == "active"


async def test_completing_twice_changes_nothing(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """FR-17 in its smallest form. A double-click, or a phone retrying a
    request whose response was lost, must not count twice - the percentage
    would exceed 100.

    `newly_completed` is how the client tells the difference, so a UI can
    stay quiet rather than congratulating somebody a second time.
    """
    _, lesson_ids = await _enrolled(client, session, instructor, student, lessons=2)
    await client.post(f"/lessons/{lesson_ids[0]}/completion", headers=student)

    again = await client.post(f"/lessons/{lesson_ids[0]}/completion", headers=student)

    assert again.status_code == 200
    body = again.json()
    assert body["completed"] == 1, "a second row would make this 2"
    assert body["newly_completed"] is False


async def test_a_student_must_be_enrolled(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """403, matching the content gate. They can see the course in the
    catalogue, so pretending the lesson does not exist would contradict the
    screen they came from."""
    _, lesson_ids = await _course_with_lessons(client, session, instructor)

    response = await client.post(
        f"/lessons/{lesson_ids[0]}/completion", headers=student
    )

    assert response.status_code == 403
    assert "Enrol" in response.json()["error"]["message"]


async def test_a_withdrawn_student_cannot_record_progress(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """Withdrawing gives up the course, not just the reading."""
    course_id, lesson_ids = await _enrolled(client, session, instructor, student)
    await client.post(f"/courses/{course_id}/enrollments/withdraw", headers=student)

    response = await client.post(
        f"/lessons/{lesson_ids[0]}/completion", headers=student
    )

    assert response.status_code == 403


async def test_a_lesson_that_does_not_exist_is_404(
    client: AsyncClient, student: dict
) -> None:
    response = await client.post(f"/lessons/{uuid4()}/completion", headers=student)

    assert response.status_code == 404


async def test_a_draft_course_hides_its_lessons_from_completion_too(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """404, not 403. An unpublished course is invisible, and a different
    answer here would tell a stranger that a lesson exists."""
    body = {"title": "Secret", "description": "x"}
    course_id = (await client.post("/courses", json=body, headers=instructor)).json()[
        "id"
    ]
    module_id = (
        await client.post(
            f"/courses/{course_id}/modules", json={"title": "M"}, headers=instructor
        )
    ).json()["id"]
    lesson_id = (
        await client.post(
            f"/modules/{module_id}/lessons",
            json={"title": "L", "content": "x"},
            headers=instructor,
        )
    ).json()["id"]

    response = await client.post(f"/lessons/{lesson_id}/completion", headers=student)

    assert response.status_code == 404


# --- FR-15, finishing the course -------------------------------------------


async def test_finishing_every_lesson_completes_the_course(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """FR-15, and the moment `completed` becomes reachable at all.

    `course_completed` is true only on the request that finished it, which
    is when a client shows a banner. `status` says whether it is complete,
    which stays true on every later read.
    """
    _, lesson_ids = await _enrolled(client, session, instructor, student, lessons=2)

    first = await client.post(f"/lessons/{lesson_ids[0]}/completion", headers=student)
    assert first.json()["course_completed"] is False

    last = await client.post(f"/lessons/{lesson_ids[1]}/completion", headers=student)

    body = last.json()
    assert body["course_completed"] is True
    assert body["status"] == "completed"
    assert body["percent"] == 100
    # The CHECK constraint makes these inseparable:
    # (status = 'completed') = (completed_at IS NOT NULL)
    assert body["completed_at"] is not None


async def test_a_completed_course_refuses_further_progress(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """409, and this is also the answer to "what if an instructor adds a
    lesson after someone finishes?".

    They cannot complete it and the course stays complete. Re-opening would
    retract an achievement nobody asked to retract - and worse, it would
    reach into a different course, because FR-08 reads `completed` for
    prerequisites. Editing one course must not invalidate a student's
    enrollment in another.
    """
    course_id, lesson_ids = await _enrolled(
        client, session, instructor, student, lessons=1
    )
    await client.post(f"/lessons/{lesson_ids[0]}/completion", headers=student)

    module_id = (
        await client.get(f"/courses/{course_id}", headers=instructor)
    ).json()["modules"][0]["id"]
    added = (
        await client.post(
            f"/modules/{module_id}/lessons",
            json={"title": "Added later", "content": "x"},
            headers=instructor,
        )
    ).json()["id"]

    response = await client.post(f"/lessons/{added}/completion", headers=student)

    assert response.status_code == 409
    assert "already completed" in response.json()["error"]["message"]


async def test_completing_a_course_does_not_lock_the_student_out(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """The bug FR-15 would have shipped with.

    `may_read_content` asked `get_active`, written in Module 2 when
    `completed` was unreachable and active was the only status that
    occurred. The moment a student finished a course, every lesson in it
    went back to content_locked - taking the material away at the exact
    moment they earned it.

    Withdrawing still locks it, and that distinction is the whole point:
    one gave up, the other finished.
    """
    course_id, lesson_ids = await _enrolled(
        client, session, instructor, student, lessons=1
    )
    await client.post(f"/lessons/{lesson_ids[0]}/completion", headers=student)

    lesson = (await client.get(f"/lessons/{lesson_ids[0]}", headers=student)).json()
    assert lesson["content"] == CONTENT
    assert lesson["content_locked"] is False

    course = (await client.get(f"/courses/{course_id}", headers=student)).json()
    assert course["modules"][0]["lessons"][0]["content"] == CONTENT


async def test_completing_frees_the_seat_while_keeping_the_material(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """Two rules that sound contradictory and are not.

    Capacity is how many people can study *at once*, so a finished student
    releases their seat. Access is about having earned the material, so they
    keep reading. Different questions, which is why they are different
    checks - get_active for the seat, get_for_content for the content.
    """
    course_id, lesson_ids = await _enrolled(
        client, session, instructor, student, lessons=1, capacity=1
    )
    await register(client, "second@example.com", ["student"])
    second = await auth_header(client, "second@example.com")
    full = await client.post(
        f"/courses/{course_id}/enrollments/enroll", headers=second
    )
    assert full.status_code == 409, "capacity 1, already taken"

    await client.post(f"/lessons/{lesson_ids[0]}/completion", headers=student)

    now_free = await client.post(
        f"/courses/{course_id}/enrollments/enroll", headers=second
    )
    assert now_free.status_code == 201

    still_readable = await client.get(f"/lessons/{lesson_ids[0]}", headers=student)
    assert still_readable.json()["content"] == CONTENT


async def test_completing_satisfies_a_prerequisite(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """FR-08 has said "completed, not merely started" since Module 2 and has
    never been exercised end to end, because nothing could reach `completed`.
    This is the first time that rule runs for real."""
    basics_id, basics_lessons = await _enrolled(
        client, session, instructor, student, lessons=1
    )
    advanced_id, _ = await _course_with_lessons(client, session, instructor)
    await client.patch(
        f"/courses/{advanced_id}",
        json={"prerequisite_ids": [basics_id]},
        headers=instructor,
    )

    blocked = await client.post(
        f"/courses/{advanced_id}/enrollments/enroll", headers=student
    )
    assert blocked.status_code == 409

    await client.post(f"/lessons/{basics_lessons[0]}/completion", headers=student)

    allowed = await client.post(
        f"/courses/{advanced_id}/enrollments/enroll", headers=student
    )
    assert allowed.status_code == 201


# --- reading progress -------------------------------------------------------


async def test_progress_starts_at_zero(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """Enrolling creates no lesson_progress rows, and should not. A row
    means "this lesson was finished"; zero rows is the honest way to say
    nothing has been."""
    course_id, _ = await _enrolled(client, session, instructor, student, lessons=4)

    body = (await client.get(f"/courses/{course_id}/progress", headers=student)).json()

    assert body == {
        "course_id": course_id,
        "enrollment_id": body["enrollment_id"],
        "status": "active",
        "completed": 0,
        "total": 4,
        "percent": 0,
        "completed_at": None,
    }


async def test_the_percentage_rounds_down(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """2 of 3 is 66, not 67. Rounding up would show 100% to somebody with a
    lesson left, which is worse than being a point mean."""
    course_id, lesson_ids = await _enrolled(
        client, session, instructor, student, lessons=3
    )
    for lesson_id in lesson_ids[:2]:
        await client.post(f"/lessons/{lesson_id}/completion", headers=student)

    body = (await client.get(f"/courses/{course_id}/progress", headers=student)).json()

    assert (body["completed"], body["total"]) == (2, 3)
    assert body["percent"] == 66


async def test_adding_a_lesson_lowers_an_unfinished_percentage(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """Why the percentage is counted rather than stored.

    A stored figure would still say 50% after the course grew, and nothing
    would recalculate it. "This person finished that lesson" stays true
    whatever changes around it.
    """
    course_id, lesson_ids = await _enrolled(
        client, session, instructor, student, lessons=2
    )
    await client.post(f"/lessons/{lesson_ids[0]}/completion", headers=student)
    assert (
        await client.get(f"/courses/{course_id}/progress", headers=student)
    ).json()["percent"] == 50

    module_id = (
        await client.get(f"/courses/{course_id}", headers=instructor)
    ).json()["modules"][0]["id"]
    await client.post(
        f"/modules/{module_id}/lessons",
        json={"title": "Third", "content": "x"},
        headers=instructor,
    )

    body = (await client.get(f"/courses/{course_id}/progress", headers=student)).json()
    assert (body["completed"], body["total"]) == (1, 3)
    assert body["percent"] == 33


async def test_a_finished_student_can_still_read_their_progress(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """Recording progress is refused after completion; reading it is not.
    There is nothing odd about looking at a finished course and seeing
    100%."""
    course_id, lesson_ids = await _enrolled(
        client, session, instructor, student, lessons=1
    )
    await client.post(f"/lessons/{lesson_ids[0]}/completion", headers=student)

    body = (await client.get(f"/courses/{course_id}/progress", headers=student)).json()

    assert body["percent"] == 100
    assert body["status"] == "completed"


async def test_progress_needs_an_enrollment(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    course_id, _ = await _course_with_lessons(client, session, instructor)

    response = await client.get(f"/courses/{course_id}/progress", headers=student)

    assert response.status_code == 403


async def test_progress_is_per_student(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """Two students, one course, two different answers. The thing being
    recorded is a fact about a person, not a property of the lesson - which
    is why the endpoint is `completion` and not `status`."""
    course_id, lesson_ids = await _enrolled(
        client, session, instructor, student, lessons=2
    )
    await register(client, "other@example.com", ["student"])
    other = await auth_header(client, "other@example.com")
    await client.post(f"/courses/{course_id}/enrollments/enroll", headers=other)

    await client.post(f"/lessons/{lesson_ids[0]}/completion", headers=student)

    mine = (await client.get(f"/courses/{course_id}/progress", headers=student)).json()
    theirs = (
        await client.get(f"/courses/{course_id}/progress", headers=other)
    ).json()
    assert mine["percent"] == 50
    assert theirs["percent"] == 0


async def test_re_enrolling_starts_progress_fresh(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """lesson_progress points at the enrollment, not the student, and this
    is what that buys. A second attempt at a course is a different
    enrollment, so it does not inherit the first attempt's work."""
    course_id, lesson_ids = await _enrolled(
        client, session, instructor, student, lessons=2
    )
    await client.post(f"/lessons/{lesson_ids[0]}/completion", headers=student)
    await client.post(f"/courses/{course_id}/enrollments/withdraw", headers=student)

    await client.post(f"/courses/{course_id}/enrollments/enroll", headers=student)

    body = (await client.get(f"/courses/{course_id}/progress", headers=student)).json()
    assert body["completed"] == 0, "the old enrollment's rows are not theirs any more"


async def test_the_old_enrollments_rows_are_still_there(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """The flip side: withdrawing keeps the history FR-19's analytics will
    need. The rows are not deleted, they belong to a closed enrollment."""
    course_id, lesson_ids = await _enrolled(
        client, session, instructor, student, lessons=2
    )
    await client.post(f"/lessons/{lesson_ids[0]}/completion", headers=student)
    await client.post(f"/courses/{course_id}/enrollments/withdraw", headers=student)

    from sqlalchemy import func, select

    from smartcourse.infra.db.models.enrollment import LessonProgress

    total = await session.scalar(
        select(func.count(LessonProgress.id))
        .join(Enrollment, Enrollment.id == LessonProgress.enrollment_id)
        .where(Enrollment.course_id == UUID(course_id))
    )
    assert total == 1
