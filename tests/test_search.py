"""Full-text search. FR-05, FR-10c.

Postgres does the work. These check the two things that are ours: that
search respects the same visibility rules as listing, and that results come
back best-match first rather than in whatever order the table happens to
hold them.
"""

from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse.infra.db.models.course import Course
from tests.conftest import auth_header, register


async def _course(
    client: AsyncClient, headers: dict, title: str, description: str = "x"
) -> str:
    response = await client.post(
        "/courses", json={"title": title, "description": description}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _publish(session: AsyncSession, course_id: str) -> None:
    course = await session.get(Course, UUID(course_id))
    course.status = "ready"
    await session.flush()


async def _lesson(client: AsyncClient, headers: dict, course_id: str, **lesson) -> str:
    module = await client.post(
        f"/courses/{course_id}/modules", json={"title": "Module"}, headers=headers
    )
    response = await client.post(
        f"/modules/{module.json()['id']}/lessons", json=lesson, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --- searching courses, FR-05 ----------------------------------------------


async def test_it_finds_a_course_by_a_word_in_its_title(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    wanted = await _course(client, instructor, "Advanced Python Programming")
    await _course(client, instructor, "Introduction to Gardening")
    await _publish(session, wanted)

    response = await client.get("/courses?q=python", headers=student)

    assert response.status_code == 200
    assert [c["id"] for c in response.json()["items"]] == [wanted]


async def test_it_finds_a_course_by_its_description(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    wanted = await _course(
        client, instructor, "Course One", description="All about recursion"
    )
    await _publish(session, wanted)

    response = await client.get("/courses?q=recursion", headers=student)

    assert [c["id"] for c in response.json()["items"]] == [wanted]


async def test_a_different_form_of_the_word_still_matches(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """English stemming: "recursive" and "recursion" reduce to the same root.

    This is the whole reason for a tsvector rather than LIKE '%...%'. A
    student searching for a word they half-remember still finds the course.
    """
    wanted = await _course(
        client, instructor, "Algorithms", description="Covers recursion in depth"
    )
    await _publish(session, wanted)

    response = await client.get("/courses?q=recursive", headers=student)

    assert [c["id"] for c in response.json()["items"]] == [wanted]


async def test_a_title_match_outranks_a_description_match(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    """setweight puts titles in band A and bodies in band B, and ts_rank
    reads those bands. A course *called* Python beats one that mentions it
    in passing - which is what anyone typing one word expects."""
    mentions = await _course(
        client, instructor, "Web Development", description="We use python here"
    )
    titled = await _course(client, instructor, "Python Basics")
    await _publish(session, mentions)
    await _publish(session, titled)

    response = await client.get("/courses?q=python", headers=student)

    ids = [c["id"] for c in response.json()["items"]]
    assert ids == [titled, mentions]


async def test_no_match_is_an_empty_page_not_an_error(
    client: AsyncClient, student: dict
) -> None:
    response = await client.get("/courses?q=xylophone", headers=student)

    assert response.status_code == 200
    assert response.json()["total"] == 0


async def test_odd_input_does_not_break_it(
    client: AsyncClient, student: dict
) -> None:
    """websearch_to_tsquery rather than to_tsquery, which treats & and | as
    operators and raises a syntax error on a stray one. A user typing into
    a search box should never produce a 500."""
    for query in ["a & b", "| or", '"unclosed quote', "-", "((("]:
        response = await client.get(f"/courses?q={query}", headers=student)
        assert response.status_code == 200, (query, response.text)


# --- search obeys visibility -----------------------------------------------


async def test_search_cannot_surface_a_draft_to_a_student(
    client: AsyncClient, instructor: dict, student: dict
) -> None:
    """The one that matters.

    Search is a filter on the same query listing uses, so the visibility
    rules apply to it. Built as a separate query path it would have been an
    easy way around FR-05 - and the kind of hole nobody notices, because
    the listing endpoint keeps behaving correctly.
    """
    await _course(client, instructor, "Secret Python Project")

    response = await client.get("/courses?q=python", headers=student)

    assert response.json()["total"] == 0


async def test_an_instructor_can_search_their_own_drafts(
    client: AsyncClient, instructor: dict
) -> None:
    draft = await _course(client, instructor, "Unpublished Python Notes")

    response = await client.get("/courses?q=python", headers=instructor)

    assert [c["id"] for c in response.json()["items"]] == [draft]


async def test_one_instructor_cannot_search_anothers_drafts(
    client: AsyncClient, instructor: dict
) -> None:
    await _course(client, instructor, "Private Python Draft")
    await register(client, "other@example.com", ["instructor"])
    other = await auth_header(client, "other@example.com")

    response = await client.get("/courses?q=python", headers=other)

    assert response.json()["total"] == 0


# --- searching lessons, FR-10c ---------------------------------------------


async def test_lessons_list_in_reading_order_without_a_query(
    client: AsyncClient, instructor: dict, session: AsyncSession
) -> None:
    course_id = await _course(client, instructor, "Course")
    await _lesson(client, instructor, course_id, title="First", content="alpha")
    await _lesson(client, instructor, course_id, title="Second", content="beta")

    response = await client.get(f"/courses/{course_id}/lessons", headers=instructor)

    assert response.status_code == 200
    assert [item["title"] for item in response.json()["items"]] == ["First", "Second"]


async def test_it_finds_a_lesson_by_its_content(
    client: AsyncClient, instructor: dict
) -> None:
    course_id = await _course(client, instructor, "Course")
    await _lesson(client, instructor, course_id, title="Lesson A", content="about cats")
    wanted = await _lesson(
        client, instructor, course_id, title="Lesson B", content="about recursion"
    )

    response = await client.get(
        f"/courses/{course_id}/lessons?q=recursion", headers=instructor
    )

    assert [item["id"] for item in response.json()["items"]] == [wanted]


async def test_a_student_cannot_search_lessons_in_a_draft(
    client: AsyncClient, instructor: dict, student: dict
) -> None:
    """404, not an empty list. The visibility check is on the course, and a
    course the student cannot see does not exist as far as they know -
    saying "no results" would confirm it does."""
    course_id = await _course(client, instructor, "Draft")
    await _lesson(client, instructor, course_id, title="Secret", content="x")

    response = await client.get(
        f"/courses/{course_id}/lessons?q=secret", headers=student
    )

    assert response.status_code == 404


async def test_a_student_can_search_lessons_in_a_published_course(
    client: AsyncClient, instructor: dict, student: dict, session: AsyncSession
) -> None:
    course_id = await _course(client, instructor, "Published")
    wanted = await _lesson(
        client, instructor, course_id, title="Recursion", content="x"
    )
    await _publish(session, course_id)

    response = await client.get(
        f"/courses/{course_id}/lessons?q=recursion", headers=student
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [wanted]
