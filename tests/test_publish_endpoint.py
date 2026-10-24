"""Who may publish a course, and what they are told when they may not.

The whole permission matrix, because the gap in it was found by hand rather
than by a test: a student publishing a course they could plainly read in the
catalogue was told "No course with that id".

Every other instructor endpoint declares `dependencies=[InstructorOnly]`;
publishing did not, so a student reached the ownership check and fell into
the 404 meant for courses they cannot see. The route now asks "are you an
instructor at all" and the service asks "is this your course", which is the
split the rest of the codebase already uses.

No Temporal here. conftest substitutes a stub client, so these test the
endpoint's decisions rather than whether a workflow then ran -
tests/test_publish_workflow.py covers that against a real server.
"""

from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse.infra.db.models.course import Course
from smartcourse.infra.db.models.user import User

from tests.conftest import auth_header, register


async def _course(client: AsyncClient, headers: dict, title: str = "Course") -> str:
    response = await client.post(
        "/courses", json={"title": title, "description": "x"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _set_status(session: AsyncSession, course_id: str, status: str) -> None:
    course = await session.get(Course, UUID(course_id))
    course.status = status
    await session.flush()


# --- the owner -------------------------------------------------------------


async def test_the_owner_can_publish_a_draft(
    client: AsyncClient, instructor: dict
) -> None:
    """202, and the body says where to look rather than returning the course.

    At this instant the status is still `draft` - the workflow has not run
    its first activity - so handing back a course saying draft would read
    like the request was ignored.
    """
    course_id = await _course(client, instructor)

    response = await client.post(f"/courses/{course_id}/publish", headers=instructor)

    assert response.status_code == 202
    body = response.json()
    assert body["course_id"] == course_id
    assert body["workflow_id"] == f"publish-course-{course_id}"
    assert body["status_url"].endswith(course_id)


async def test_a_failed_course_can_be_published_again(
    client: AsyncClient, instructor: dict, session: AsyncSession
) -> None:
    """A failed publish leaves a course that needs another attempt. Refusing
    would mean the only recovery is editing the database."""
    course_id = await _course(client, instructor)
    await _set_status(session, course_id, "failed")

    response = await client.post(f"/courses/{course_id}/publish", headers=instructor)

    assert response.status_code == 202


async def test_publishing_an_already_published_course_is_409(
    client: AsyncClient, instructor: dict, session: AsyncSession
) -> None:
    course_id = await _course(client, instructor)
    await _set_status(session, course_id, "ready")

    response = await client.post(f"/courses/{course_id}/publish", headers=instructor)

    assert response.status_code == 409
    body = response.json()["error"]
    assert "ready" in body["message"]
    # Says what would work, not only what did not.
    assert body["details"]["publishable_from"] == ["draft", "failed"]


# --- the bug this file exists for ------------------------------------------


async def test_a_student_is_told_they_need_the_instructor_role(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """403 with a useful reason, not 404.

    The course is published, so this student can read it, search for it and
    see it in the catalogue. Telling them it does not exist contradicts the
    screen they came from - and "this belongs to another instructor" would
    be true but misleading, since owning one would not help a student.
    """
    course_id = await _course(client, instructor)
    await _set_status(session, course_id, "ready")

    response = await client.post(f"/courses/{course_id}/publish", headers=student)

    assert response.status_code == 403
    body = response.json()["error"]
    assert body["code"] == "permission_denied"
    assert body["details"]["your_roles"] == ["student"]


async def test_the_student_really_can_see_the_course_they_were_refused(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """The assertion that makes the one above mean something.

    A 403 is only the right answer because the course is visible. If this
    GET returned 404 too, the refusal would be consistent and 404 would be
    correct.
    """
    course_id = await _course(client, instructor)
    await _set_status(session, course_id, "ready")

    assert (
        await client.get(f"/courses/{course_id}", headers=student)
    ).status_code == 200
    assert (
        await client.post(f"/courses/{course_id}/publish", headers=student)
    ).status_code == 403


# --- another instructor ----------------------------------------------------


async def test_another_instructor_publishing_a_visible_course_is_403(
    client: AsyncClient, instructor: dict, session: AsyncSession
) -> None:
    """They can see it, so saying it does not exist would be a lie."""
    course_id = await _course(client, instructor)
    await _set_status(session, course_id, "ready")
    await register(client, "other@example.com", ["instructor"])
    other = await auth_header(client, "other@example.com")

    response = await client.post(f"/courses/{course_id}/publish", headers=other)

    assert response.status_code == 403
    assert "another instructor" in response.json()["error"]["message"]


async def test_another_instructor_publishing_a_draft_is_404(
    client: AsyncClient, instructor: dict
) -> None:
    """They cannot see it, so confirming it exists is itself information -
    an unannounced draft is something an instructor may not want known."""
    course_id = await _course(client, instructor)
    await register(client, "other@example.com", ["instructor"])
    other = await auth_header(client, "other@example.com")

    response = await client.post(f"/courses/{course_id}/publish", headers=other)

    assert response.status_code == 404


# --- the rest --------------------------------------------------------------


async def test_an_admin_can_publish_anyones_course(
    client: AsyncClient, instructor: dict, session: AsyncSession
) -> None:
    """Consistent with the rest. An admin can already see, edit and delete
    any course; blocking publishing alone would be an odd line to draw.

    The role is granted directly here because admin is not self-assignable
    - that is the point of it - and the seed command is the only other way
    to create one.
    """
    course_id = await _course(client, instructor)

    created = await register(client, "boss@example.com", ["instructor"])
    boss = await session.get(User, UUID(created["id"]))
    boss.roles = ["admin"]
    await session.flush()

    admin = await auth_header(client, "boss@example.com")
    response = await client.post(f"/courses/{course_id}/publish", headers=admin)

    assert response.status_code == 202


async def test_publishing_a_course_that_does_not_exist_is_404(
    client: AsyncClient, instructor: dict
) -> None:
    response = await client.post(f"/courses/{uuid4()}/publish", headers=instructor)

    assert response.status_code == 404


async def test_anonymous_cannot_publish(client: AsyncClient, instructor: dict) -> None:
    course_id = await _course(client, instructor)

    response = await client.post(f"/courses/{course_id}/publish")

    assert response.status_code == 401
