"""Course, module and lesson management. FR-03, FR-04, FR-05."""

from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse.infra.db.models.course import Course

from tests.conftest import auth_header, register


async def _make_course(client: AsyncClient, headers: dict, title: str = "Python") -> str:
    response = await client.post(
        "/courses", json={"title": title, "description": "x"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _publish(session: AsyncSession, course_id: str) -> None:
    """Mark a course ready.

    Done directly, because publishing is a Module 2 workflow and there is no
    endpoint for it yet.
    """
    course = await session.get(Course, UUID(course_id))
    course.status = "ready"
    await session.flush()


# --- permissions -----------------------------------------------------------


async def test_student_cannot_create_a_course(
    client: AsyncClient, student: dict
) -> None:
    """FR-03. The check that matters most in Module 1."""
    response = await client.post(
        "/courses", json={"title": "Sneaky"}, headers=student
    )

    assert response.status_code == 403
    body = response.json()["error"]
    assert body["code"] == "permission_denied"
    # Tells the caller what they have, so the failure is diagnosable.
    assert body["details"]["your_roles"] == ["student"]


async def test_anonymous_cannot_create_a_course(client: AsyncClient) -> None:
    response = await client.post("/courses", json={"title": "Sneaky"})

    assert response.status_code == 401


async def test_instructor_cannot_edit_another_instructors_course(
    client: AsyncClient, instructor: dict
) -> None:
    """Role gets you through the door; ownership decides which course.

    Enforced in the service rather than the route, because it must hold for
    callers that never touch a route.
    """
    course_id = await _make_course(client, instructor)
    await register(client, "rival@example.com", ["instructor"])
    rival = await auth_header(client, "rival@example.com")

    response = await client.patch(
        f"/courses/{course_id}", json={"title": "Hijacked"}, headers=rival
    )

    assert response.status_code == 403


async def test_student_can_read_published_courses(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """Reading is open to anyone signed in; only writing is instructor-only.

    The course has to be published first. An earlier version of this test
    used a draft and passed - which is how the missing visibility rule went
    unnoticed. The test was asserting the bug.
    """
    course_id = await _make_course(client, instructor)
    await _publish(session, course_id)

    response = await client.get("/courses", headers=student)

    assert response.status_code == 200
    assert response.json()["total"] == 1


# --- creating and reading --------------------------------------------------


async def test_new_courses_start_as_draft(
    client: AsyncClient, instructor: dict
) -> None:
    """Status is an outcome, not a field a caller sets. You publish a course;
    you do not set it to ready."""
    response = await client.post(
        "/courses", json={"title": "Python", "capacity": 30}, headers=instructor
    )

    assert response.json()["status"] == "draft"
    assert response.json()["published_at"] is None


async def test_capacity_may_be_omitted_meaning_unlimited(
    client: AsyncClient, instructor: dict
) -> None:
    """Null is not zero. Zero would mean a course nobody can ever join."""
    response = await client.post(
        "/courses", json={"title": "Unlimited"}, headers=instructor
    )

    assert response.json()["capacity"] is None


async def test_zero_capacity_is_rejected(
    client: AsyncClient, instructor: dict
) -> None:
    response = await client.post(
        "/courses", json={"title": "Impossible", "capacity": 0}, headers=instructor
    )

    assert response.status_code == 422


async def test_reading_a_course_includes_its_modules_and_lessons(
    client: AsyncClient, instructor: dict
) -> None:
    """Reads are generous - one call returns the whole tree - because reading
    cannot corrupt anything."""
    course_id = await _make_course(client, instructor)
    module = await client.post(
        f"/courses/{course_id}/modules", json={"title": "M1"}, headers=instructor
    )
    module_id = module.json()["id"]
    await client.post(
        f"/modules/{module_id}/lessons",
        json={"title": "L1", "content": "hello"},
        headers=instructor,
    )

    response = await client.get(f"/courses/{course_id}", headers=instructor)

    assert response.status_code == 200
    body = response.json()
    assert len(body["modules"]) == 1
    assert body["modules"][0]["lessons"][0]["title"] == "L1"


async def test_unknown_course_is_404(client: AsyncClient, instructor: dict) -> None:
    response = await client.get(
        "/courses/00000000-0000-0000-0000-000000000000", headers=instructor
    )

    assert response.status_code == 404


# --- ordering --------------------------------------------------------------


async def test_modules_are_appended_in_order(
    client: AsyncClient, instructor: dict
) -> None:
    course_id = await _make_course(client, instructor)

    first = await client.post(
        f"/courses/{course_id}/modules", json={"title": "First"}, headers=instructor
    )
    second = await client.post(
        f"/courses/{course_id}/modules", json={"title": "Second"}, headers=instructor
    )

    assert first.json()["position"] == 0
    assert second.json()["position"] == 1


async def test_reordering_modules_rewrites_every_position(
    client: AsyncClient, instructor: dict
) -> None:
    """Only possible because the unique constraint is DEFERRABLE.

    Both rows briefly hold the same position mid-transaction. Checked after
    each statement, this would fail halfway; checked at COMMIT, it succeeds.
    """
    course_id = await _make_course(client, instructor)
    first = (
        await client.post(
            f"/courses/{course_id}/modules", json={"title": "First"}, headers=instructor
        )
    ).json()["id"]
    second = (
        await client.post(
            f"/courses/{course_id}/modules",
            json={"title": "Second"},
            headers=instructor,
        )
    ).json()["id"]

    response = await client.patch(
        f"/courses/{course_id}",
        json={"module_order": [second, first]},
        headers=instructor,
    )
    assert response.status_code == 200

    modules = (
        await client.get(f"/courses/{course_id}", headers=instructor)
    ).json()["modules"]
    assert [m["title"] for m in modules] == ["Second", "First"]
    assert [m["position"] for m in modules] == [0, 1]


async def test_partial_reorder_is_rejected(
    client: AsyncClient, instructor: dict
) -> None:
    """The complete order or nothing. A partial list would silently leave
    some modules at stale positions."""
    course_id = await _make_course(client, instructor)
    first = (
        await client.post(
            f"/courses/{course_id}/modules", json={"title": "First"}, headers=instructor
        )
    ).json()["id"]
    await client.post(
        f"/courses/{course_id}/modules", json={"title": "Second"}, headers=instructor
    )

    response = await client.patch(
        f"/courses/{course_id}", json={"module_order": [first]}, headers=instructor
    )

    assert response.status_code == 422


# --- updating and deleting -------------------------------------------------


async def test_patch_only_changes_what_was_sent(
    client: AsyncClient, instructor: dict
) -> None:
    """An omitted field keeps its value. That is what PATCH means, and it is
    why a field cannot be cleared by accident."""
    course_id = await _make_course(client, instructor)

    response = await client.patch(
        f"/courses/{course_id}", json={"title": "Renamed"}, headers=instructor
    )

    assert response.json()["title"] == "Renamed"
    assert response.json()["description"] == "x"  # untouched


async def test_deleting_a_course_removes_its_modules_and_lessons(
    client: AsyncClient, instructor: dict
) -> None:
    """The foreign keys cascade: a module without a course is unreachable."""
    course_id = await _make_course(client, instructor)
    module_id = (
        await client.post(
            f"/courses/{course_id}/modules", json={"title": "M"}, headers=instructor
        )
    ).json()["id"]

    deleted = await client.delete(f"/courses/{course_id}", headers=instructor)
    assert deleted.status_code == 204

    orphan = await client.patch(
        f"/modules/{module_id}", json={"title": "Still here?"}, headers=instructor
    )
    assert orphan.status_code == 404


async def test_deleting_a_lesson_leaves_the_module(
    client: AsyncClient, instructor: dict
) -> None:
    course_id = await _make_course(client, instructor)
    module_id = (
        await client.post(
            f"/courses/{course_id}/modules", json={"title": "M"}, headers=instructor
        )
    ).json()["id"]
    lesson_id = (
        await client.post(
            f"/modules/{module_id}/lessons", json={"title": "L"}, headers=instructor
        )
    ).json()["id"]

    assert (
        await client.delete(f"/lessons/{lesson_id}", headers=instructor)
    ).status_code == 204

    course = (await client.get(f"/courses/{course_id}", headers=instructor)).json()
    assert len(course["modules"]) == 1
    assert course["modules"][0]["lessons"] == []


# --- listing ---------------------------------------------------------------


async def test_listing_is_paginated(client: AsyncClient, instructor: dict) -> None:
    for index in range(3):
        await _make_course(client, instructor, title=f"Course {index}")

    response = await client.get("/courses?limit=2&offset=0", headers=instructor)

    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


async def test_listing_can_filter_by_status(
    client: AsyncClient, instructor: dict
) -> None:
    await _make_course(client, instructor)

    drafts = await client.get("/courses?status=draft", headers=instructor)
    ready = await client.get("/courses?status=ready", headers=instructor)

    assert drafts.json()["total"] == 1
    assert ready.json()["total"] == 0


# --- visibility -----------------------------------------------------------
#
# FR-05 promises students browse *published* courses. A draft is unfinished
# by definition, so showing one to a student exposes work not meant to be
# seen - including other instructors'.


async def test_student_cannot_see_a_draft_course(
    client: AsyncClient, instructor: dict, student: dict
) -> None:
    await _make_course(client, instructor)

    listed = await client.get("/courses", headers=student)

    assert listed.json()["total"] == 0


async def test_student_sees_a_published_course(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    course_id = await _make_course(client, instructor)
    await _publish(session, course_id)

    listed = await client.get("/courses", headers=student)

    assert listed.json()["total"] == 1


async def test_student_cannot_fetch_a_draft_by_id(
    client: AsyncClient, instructor: dict, student: dict
) -> None:
    """Filtering the list is pointless if the id still works.

    404 rather than 403: a 403 would confirm the course exists, and that an
    instructor has something unannounced in draft is itself information.
    """
    course_id = await _make_course(client, instructor)

    response = await client.get(f"/courses/{course_id}", headers=student)

    assert response.status_code == 404


async def test_instructor_sees_their_own_drafts(
    client: AsyncClient, instructor: dict
) -> None:
    await _make_course(client, instructor)

    listed = await client.get("/courses", headers=instructor)

    assert listed.json()["total"] == 1


async def test_instructor_cannot_see_another_instructors_draft(
    client: AsyncClient, instructor: dict
) -> None:
    """Own drafts, not everyone's."""
    await _make_course(client, instructor)
    await register(client, "rival@example.com", ["instructor"])
    rival = await auth_header(client, "rival@example.com")

    listed = await client.get("/courses", headers=rival)

    assert listed.json()["total"] == 0


async def test_a_student_asking_for_drafts_is_refused(
    client: AsyncClient, instructor: dict, student: dict
) -> None:
    """403, not an empty page.

    An empty result is indistinguishable from "there are none", and a client
    would render "no courses found" - which is untrue. Saying plainly that
    the filter is not permitted is the honest answer, and reveals nothing:
    that a draft state exists is already in the API documentation.
    """
    await _make_course(client, instructor)

    listed = await client.get("/courses?status=draft", headers=student)

    assert listed.status_code == 403
    assert listed.json()["error"]["details"]["allowed"] == ["ready"]


async def test_an_instructor_may_filter_by_draft(
    client: AsyncClient, instructor: dict
) -> None:
    """Allowed, because the visibility rule underneath still limits it to
    their own drafts. The filter narrows; it cannot widen."""
    await _make_course(client, instructor)
    await register(client, "rival2@example.com", ["instructor"])
    rival = await auth_header(client, "rival2@example.com")
    await _make_course(client, rival, title="Rival draft")

    listed = await client.get("/courses?status=draft", headers=instructor)

    assert listed.status_code == 200
    assert listed.json()["total"] == 1  # theirs only


async def test_an_unknown_status_is_rejected_as_invalid(
    client: AsyncClient, instructor: dict
) -> None:
    """422, not 403. A status that does not exist is a malformed request,
    not a permission problem - and previously it returned an empty page,
    silently hiding the typo."""
    listed = await client.get("/courses?status=banana", headers=instructor)

    assert listed.status_code == 422
    assert "banana" in listed.json()["error"]["message"]


# --- single module and lesson reads ---------------------------------------


async def test_get_a_single_module_with_its_lessons(
    client: AsyncClient, instructor: dict
) -> None:
    course_id = await _make_course(client, instructor)
    module_id = (
        await client.post(
            f"/courses/{course_id}/modules", json={"title": "M1"}, headers=instructor
        )
    ).json()["id"]
    await client.post(
        f"/modules/{module_id}/lessons", json={"title": "L1"}, headers=instructor
    )

    response = await client.get(f"/modules/{module_id}", headers=instructor)

    assert response.status_code == 200
    body = response.json()
    assert body["course_id"] == course_id      # the parent, for context
    assert [lesson["title"] for lesson in body["lessons"]] == ["L1"]


async def test_get_a_single_lesson_carries_both_parents(
    client: AsyncClient, instructor: dict
) -> None:
    """A lesson row has no course_id - the service supplies it from the
    visibility check, so a client arriving by a direct link knows where it is
    without a second call."""
    course_id = await _make_course(client, instructor)
    module_id = (
        await client.post(
            f"/courses/{course_id}/modules", json={"title": "M1"}, headers=instructor
        )
    ).json()["id"]
    lesson_id = (
        await client.post(
            f"/modules/{module_id}/lessons",
            json={"title": "L1", "content": "hello"},
            headers=instructor,
        )
    ).json()["id"]

    response = await client.get(f"/lessons/{lesson_id}", headers=instructor)

    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "hello"
    assert body["module_id"] == module_id
    assert body["course_id"] == course_id


async def test_student_cannot_read_a_module_of_a_draft_course(
    client: AsyncClient, instructor: dict, student: dict
) -> None:
    """Otherwise this endpoint is a way round the course rule - a draft could
    be read one module at a time."""
    course_id = await _make_course(client, instructor)
    module_id = (
        await client.post(
            f"/courses/{course_id}/modules", json={"title": "M1"}, headers=instructor
        )
    ).json()["id"]

    response = await client.get(f"/modules/{module_id}", headers=student)

    assert response.status_code == 404


async def test_student_cannot_read_a_lesson_of_a_draft_course(
    client: AsyncClient, instructor: dict, student: dict
) -> None:
    course_id = await _make_course(client, instructor)
    module_id = (
        await client.post(
            f"/courses/{course_id}/modules", json={"title": "M1"}, headers=instructor
        )
    ).json()["id"]
    lesson_id = (
        await client.post(
            f"/modules/{module_id}/lessons",
            json={"title": "Secret", "content": "unreleased"},
            headers=instructor,
        )
    ).json()["id"]

    response = await client.get(f"/lessons/{lesson_id}", headers=student)

    assert response.status_code == 404


async def test_student_can_read_a_lesson_once_published(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    course_id = await _make_course(client, instructor)
    module_id = (
        await client.post(
            f"/courses/{course_id}/modules", json={"title": "M1"}, headers=instructor
        )
    ).json()["id"]
    lesson_id = (
        await client.post(
            f"/modules/{module_id}/lessons",
            json={"title": "L1", "content": "hello"},
            headers=instructor,
        )
    ).json()["id"]
    await _publish(session, course_id)

    response = await client.get(f"/lessons/{lesson_id}", headers=student)

    assert response.status_code == 200
    assert response.json()["content"] == "hello"
