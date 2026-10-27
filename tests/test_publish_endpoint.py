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

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import WorkflowExecutionStatus

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
    assert body["status_url"].endswith(f"{course_id}/publish/status")


async def test_a_failed_course_can_be_published_again(
    client: AsyncClient, instructor: dict, session: AsyncSession
) -> None:
    """A failed publish leaves a course that needs another attempt. Refusing
    would mean the only recovery is editing the database."""
    course_id = await _course(client, instructor)
    await _set_status(session, course_id, "failed")

    response = await client.post(f"/courses/{course_id}/publish", headers=instructor)

    assert response.status_code == 202


async def test_a_ready_course_can_be_published_again(
    client: AsyncClient, instructor: dict, session: AsyncSession
) -> None:
    """Republishing is the point, not an edge case.

    Part A describes the trigger as "when an instructor publishes *or
    updates* a course", and the problem it opens with is that publishing is
    slow for new courses "and update existing ones". FR-11a - reusing
    chunks whose content has not changed - only earns its complexity if a
    second publish can happen at all.

    This was refused until now, because PUBLISHABLE_STATUSES listed only
    draft and failed. The reasoning written beside it was about excluding
    `publishing` so two publishes cannot overlap; `ready` fell outside the
    allowlist without anybody deciding it should.
    """
    course_id = await _course(client, instructor)
    await _set_status(session, course_id, "ready")

    response = await client.post(f"/courses/{course_id}/publish", headers=instructor)

    assert response.status_code == 202


async def test_a_course_mid_publish_cannot_be_published_again(
    client: AsyncClient, instructor: dict, session: AsyncSession
) -> None:
    """The exclusion that was always the real one.

    Temporal refuses a second workflow with a live id, so this is not what
    enforces it - but a 409 naming the status is a better answer than
    whatever Temporal's rejection would turn into.
    """
    course_id = await _course(client, instructor)
    await _set_status(session, course_id, "publishing")

    response = await client.post(f"/courses/{course_id}/publish", headers=instructor)

    assert response.status_code == 409
    body = response.json()["error"]
    assert "publishing" in body["message"]
    # Says what would work, not only what did not.
    assert body["details"]["publishable_from"] == ["draft", "failed", "ready"]


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


# --- how did it go? --------------------------------------------------------
#
# The endpoint exists because a failed republish is invisible in the
# course's own status: it stays `ready`, which is correct - the live
# material is untouched and last time's chunks still serve - and therefore
# useless as an answer to "did my publish work".
#
# No Temporal here either. The stub client's describe() reports NOT_FOUND
# unless a test sets one, which is also the real answer once a history ages
# out of retention.


async def test_the_status_url_from_a_publish_can_be_fetched(
    client: AsyncClient, instructor: dict
) -> None:
    """The 202 points at something that answers. It used to point at the
    course, which stopped being able to the moment a failed republish
    started leaving the course `ready`."""
    course_id = await _course(client, instructor)
    accepted = (
        await client.post(f"/courses/{course_id}/publish", headers=instructor)
    ).json()

    path = accepted["status_url"].removeprefix("/api/v1")
    response = await client.get(path, headers=instructor)

    assert response.status_code == 200
    assert response.json()["workflow_id"] == accepted["workflow_id"]


async def test_a_course_temporal_has_no_record_of_reports_unknown(
    client: AsyncClient, instructor: dict
) -> None:
    """Two different things look the same here, and the response says so.

    Temporal is a workflow engine, not an archive - a history outlives the
    run by its retention period and no longer. So "never published" and
    "published last year" both come back as unknown, and the durable
    columns beside it are what tell them apart.
    """
    course_id = await _course(client, instructor)

    body = (
        await client.get(f"/courses/{course_id}/publish/status", headers=instructor)
    ).json()

    assert body["state"] == "unknown"
    assert body["chunks_written"] is None
    # The durable half, which is always true.
    assert body["course_status"] == "draft"
    assert body["published_at"] is None


async def test_a_finished_publish_reports_its_chunk_count(
    client: AsyncClient, instructor: dict
) -> None:
    """chunks_written is the workflow's return value, so it only exists once
    the workflow has returned. Reading it on a running one would block the
    request until the publish finished."""
    course_id = await _course(client, instructor)
    client.workflows.description = SimpleNamespace(
        status=WorkflowExecutionStatus.COMPLETED,
        start_time=datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 10, 27, 9, 0, 12, tzinfo=timezone.utc),
    )
    client.workflows.result = 14

    body = (
        await client.get(f"/courses/{course_id}/publish/status", headers=instructor)
    ).json()

    assert body["state"] == "completed"
    assert body["chunks_written"] == 14
    assert body["started_at"] is not None
    assert body["finished_at"] is not None


async def test_a_failed_republish_is_visible_here_and_nowhere_else(
    client: AsyncClient, instructor: dict, session: AsyncSession
) -> None:
    """The whole reason this endpoint was built.

    The course says `ready`, because it is - the failure touched nothing a
    reader can see. Without this endpoint the instructor has no way to find
    out that their edit never reached the chunks.
    """
    course_id = await _course(client, instructor)
    await _set_status(session, course_id, "ready")
    client.workflows.description = SimpleNamespace(
        status=WorkflowExecutionStatus.FAILED,
        start_time=datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 10, 27, 9, 0, 3, tzinfo=timezone.utc),
    )

    body = (
        await client.get(f"/courses/{course_id}/publish/status", headers=instructor)
    ).json()

    assert body["state"] == "failed"
    assert body["course_status"] == "ready"
    # Not knowable from the course, which is the point.
    assert body["chunks_written"] is None


async def test_a_student_cannot_ask_how_someone_elses_publish_went(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """Same ladder as POST /publish, so the two cannot disagree: visible
    course, wrong role, 403 rather than a 404 contradicting the catalogue."""
    course_id = await _course(client, instructor)
    await _set_status(session, course_id, "ready")

    response = await client.get(
        f"/courses/{course_id}/publish/status", headers=student
    )

    assert response.status_code == 403


async def test_another_instructor_cannot_ask_either(
    client: AsyncClient, instructor: dict, session: AsyncSession
) -> None:
    course_id = await _course(client, instructor)
    await _set_status(session, course_id, "ready")
    await register(client, "nosy@example.com", ["instructor"])
    nosy = await auth_header(client, "nosy@example.com")

    response = await client.get(f"/courses/{course_id}/publish/status", headers=nosy)

    assert response.status_code == 403
