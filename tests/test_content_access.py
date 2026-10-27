"""Who may read lesson material. FR-05, UC-02.

Seeing a course and reading it are two different permissions, and until
Module 2 they were one: any signed-in user got the full text of every
published course, which made enrolment a formality.

The split a catalogue needs:

    the syllabus    module and lesson titles, visible to anyone who can
                    see the course - it is how you decide to enrol
    the material    only for the owner, an admin, or an active enrolment

Deferred from Module 1 and recorded in STATE.md as "decide it in Module 2,
when enrollment exists".
"""

from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse.infra.db.models.course import Course
from smartcourse.infra.db.models.user import User

from tests.conftest import auth_header, register

CONTENT = "The material nobody should read without enrolling."


async def _published_course(
    client: AsyncClient, headers: dict, session: AsyncSession
) -> tuple[str, str]:
    """A published course with one lesson. Returns (course_id, lesson_id)."""
    course_id = (
        await client.post(
            "/courses", json={"title": "Gated", "description": "x"}, headers=headers
        )
    ).json()["id"]
    module_id = (
        await client.post(
            f"/courses/{course_id}/modules", json={"title": "M"}, headers=headers
        )
    ).json()["id"]
    lesson_id = (
        await client.post(
            f"/modules/{module_id}/lessons",
            json={"title": "Lesson One", "content": CONTENT},
            headers=headers,
        )
    ).json()["id"]

    course = await session.get(Course, UUID(course_id))
    course.status = "ready"
    await session.flush()
    return course_id, lesson_id


# --- the syllabus stays visible --------------------------------------------


async def test_the_structure_is_visible_without_enrolling(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """You cannot decide whether to enrol in a course you cannot see the
    shape of. Titles and order stay; the text does not."""
    course_id, _ = await _published_course(client, instructor, session)

    body = (await client.get(f"/courses/{course_id}", headers=student)).json()

    lesson = body["modules"][0]["lessons"][0]
    assert lesson["title"] == "Lesson One"
    assert lesson["content"] is None
    assert lesson["content_locked"] is True


async def test_locked_is_distinguishable_from_simply_empty(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """Null content alone is ambiguous - an unwritten lesson looks exactly
    like a withheld one. A client showing "Enrol to read this" has to be
    able to tell them apart, and guessing from the absence of text breaks
    the first time an instructor saves an empty lesson."""
    course_id, _ = await _published_course(client, instructor, session)
    module_id = (
        await client.get(f"/courses/{course_id}", headers=instructor)
    ).json()["modules"][0]["id"]
    await client.post(
        f"/modules/{module_id}/lessons",
        json={"title": "Not written yet", "content": None},
        headers=instructor,
    )

    body = (await client.get(f"/courses/{course_id}", headers=instructor)).json()

    lessons = body["modules"][0]["lessons"]
    assert all(lesson["content_locked"] is False for lesson in lessons)
    assert [lesson["content"] for lesson in lessons] == [CONTENT, None]


# --- the material is gated --------------------------------------------------


async def test_a_lesson_cannot_be_read_without_enrolling(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """403, not 404. The course is published and the student can see it in
    the catalogue, so pretending the lesson does not exist would contradict
    the screen they came from."""
    _, lesson_id = await _published_course(client, instructor, session)

    response = await client.get(f"/lessons/{lesson_id}", headers=student)

    assert response.status_code == 403
    assert "Enrol" in response.json()["error"]["message"]


async def test_enrolling_unlocks_the_material(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    course_id, lesson_id = await _published_course(client, instructor, session)

    await client.post(f"/courses/{course_id}/enrollments/enroll", headers=student)

    lesson = (await client.get(f"/lessons/{lesson_id}", headers=student)).json()
    assert lesson["content"] == CONTENT
    assert lesson["content_locked"] is False

    body = (await client.get(f"/courses/{course_id}", headers=student)).json()
    assert body["modules"][0]["lessons"][0]["content"] == CONTENT


async def test_withdrawing_locks_it_again(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """A withdrawn enrolment does not count. Keeping the material after
    leaving would make withdrawing free, and the history row exists for
    analytics rather than as a permanent key."""
    course_id, lesson_id = await _published_course(client, instructor, session)
    await client.post(f"/courses/{course_id}/enrollments/enroll", headers=student)
    assert (
        await client.get(f"/lessons/{lesson_id}", headers=student)
    ).status_code == 200

    await client.post(f"/courses/{course_id}/enrollments/withdraw", headers=student)

    assert (
        await client.get(f"/lessons/{lesson_id}", headers=student)
    ).status_code == 403


# --- who is exempt ----------------------------------------------------------


async def test_the_owning_instructor_does_not_have_to_enrol(
    client: AsyncClient, instructor: dict, session: AsyncSession
) -> None:
    """It is their course. Requiring an instructor to enrol in their own
    material to proofread it would be absurd."""
    _, lesson_id = await _published_course(client, instructor, session)

    lesson = (await client.get(f"/lessons/{lesson_id}", headers=instructor)).json()

    assert lesson["content"] == CONTENT


async def test_an_admin_does_not_have_to_enrol(
    client: AsyncClient, instructor: dict, session: AsyncSession
) -> None:
    """Consistent with the rest: an admin can already see, edit, delete and
    publish any course."""
    _, lesson_id = await _published_course(client, instructor, session)

    created = await register(client, "boss@example.com", ["instructor"])
    boss = await session.get(User, UUID(created["id"]))
    boss.roles = ["admin"]
    await session.flush()
    admin = await auth_header(client, "boss@example.com")

    lesson = (await client.get(f"/lessons/{lesson_id}", headers=admin)).json()

    assert lesson["content"] == CONTENT


async def test_another_instructor_must_enrol_like_anyone_else(
    client: AsyncClient, instructor: dict, session: AsyncSession
) -> None:
    """Being an instructor is not a key to other people's courses. UC-07 is
    the path: take the student role and enrol."""
    _, lesson_id = await _published_course(client, instructor, session)
    await register(client, "other@example.com", ["instructor"])
    other = await auth_header(client, "other@example.com")

    response = await client.get(f"/lessons/{lesson_id}", headers=other)

    assert response.status_code == 403


# --- search must not leak it ------------------------------------------------


async def test_search_results_do_not_carry_locked_content(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """The gate has to hold on every path out, not just the obvious one.

    Lesson search returns whole lessons, so without this the material
    leaks through `?q=` to anyone who can guess a word in it - and the
    course endpoint would keep looking correct.
    """
    course_id, _ = await _published_course(client, instructor, session)

    response = await client.get(
        f"/courses/{course_id}/lessons?q=material", headers=student
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert items, "the search should still find the lesson"
    assert items[0]["title"] == "Lesson One"
    assert items[0]["content"] is None
    assert items[0]["content_locked"] is True
