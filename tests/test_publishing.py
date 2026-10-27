"""Publishing a course. FR-11, FR-11a.

The service layer only - no Temporal here. These call the same functions the
activities will, which is the point of keeping the logic out of the workflow.
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse.infra.db.models.content import Chunk
from smartcourse.infra.db.models.course import Course, Lesson, Module
from smartcourse.infra.db.models.user import User
from smartcourse.infra.db.repositories import (
    ChunkRepository,
    CourseRepository,
    LessonRepository,
)
from smartcourse.services.publishing import (
    begin_publish,
    chunk_lesson,
    complete_publish,
    content_hash,
)

# Two paragraphs of 16 and 10 estimated tokens. A ceiling of 20 keeps each
# one whole and stops them sharing a chunk, so "the chunk containing FIRST"
# is a real thing these tests can point at. Checked rather than guessed - at
# a ceiling of 12 the first paragraph is itself split and no chunk contains
# it, which is how the first version of this file failed.
FIRST = "Alpha one two three four five. Bravo six seven eight nine ten."
SECOND = "Charlie eleven twelve thirteen. Delta fourteen fifteen sixteen."
BODY = f"{FIRST}\n\n{SECOND}"

SMALL = {"max_tokens": 20, "overlap_tokens": 0}


def _only(chunks: list[Chunk], containing: str) -> Chunk:
    """The one chunk holding this text.

    A plain next() raises StopIteration, which inside a coroutine surfaces
    as "RuntimeError: coroutine raised StopIteration" and says nothing
    about what went wrong. This fails with the text it was looking for.
    """
    found = [c for c in chunks if containing in c.content]
    assert len(found) == 1, (
        f"expected exactly one chunk containing {containing!r}, "
        f"got {len(found)}: {[c.content for c in chunks]}"
    )
    return found[0]


async def _lesson(session: AsyncSession, content: str | None = BODY) -> Lesson:
    """A course, a module and a lesson, written straight to the session.

    Built directly rather than through the API because what is under test is
    a service, and going through HTTP would only add noise.
    """
    tag = uuid4().hex[:8]
    teacher = User(
        email=f"pub-{tag}@example.com",
        password="x",
        full_name="Teacher",
        roles=["instructor"],
    )
    session.add(teacher)
    await session.flush()

    course = Course(instructor_id=teacher.id, title=f"Course {tag}", status="draft")
    session.add(course)
    await session.flush()

    module = Module(course_id=course.id, title="Module", position=0)
    session.add(module)
    await session.flush()

    lesson = Lesson(module_id=module.id, title="Lesson", content=content, position=0)
    session.add(lesson)
    await session.flush()
    return lesson


async def _chunks(session: AsyncSession, lesson: Lesson) -> list[Chunk]:
    return await ChunkRepository(session).existing_for_lesson(lesson.id)


# --- the basics ------------------------------------------------------------


async def test_a_lesson_becomes_chunks(session: AsyncSession) -> None:
    lesson = await _lesson(session)

    written = await chunk_lesson(ChunkRepository(session), lesson, **SMALL)

    stored = await _chunks(session, lesson)
    assert written == len(stored) > 1
    assert [c.position for c in stored] == list(range(len(stored)))
    assert all(c.token_count > 0 for c in stored)
    # Null means the text came from lessons.content rather than a file.
    assert all(c.asset_id is None for c in stored)


async def test_a_lesson_with_no_content_has_no_chunks(
    session: AsyncSession,
) -> None:
    """lessons.content is nullable, so a lesson can exist before anything is
    written in it. Publishing that must not fail."""
    lesson = await _lesson(session, content=None)

    written = await chunk_lesson(ChunkRepository(session), lesson, **SMALL)

    assert written == 0
    assert await _chunks(session, lesson) == []


# --- FR-11a, the part that matters -----------------------------------------


async def test_republishing_unchanged_content_keeps_the_same_rows(
    session: AsyncSession,
) -> None:
    """The whole of FR-11a, and the assertion is on the row *ids*.

    Deleting every chunk and reinserting would produce identical text and
    pass any test that only compared content - while throwing away each
    chunk's embedding in Module 4, for every lesson, on every republish.
    Same id means the row was never replaced.
    """
    lesson = await _lesson(session)
    chunks = ChunkRepository(session)

    await chunk_lesson(chunks, lesson, **SMALL)
    before = {c.id for c in await _chunks(session, lesson)}

    await chunk_lesson(chunks, lesson, **SMALL)
    after = {c.id for c in await _chunks(session, lesson)}

    assert before == after


async def test_editing_one_paragraph_leaves_the_other_alone(
    session: AsyncSession,
) -> None:
    """Editing one lesson must not re-embed the rest of the course, and
    editing one paragraph must not re-embed the rest of the lesson."""
    lesson = await _lesson(session)
    chunks = ChunkRepository(session)
    await chunk_lesson(chunks, lesson, **SMALL)

    untouched = _only(await _chunks(session, lesson), FIRST).id

    lesson.content = f"{FIRST}\n\nEcho seventeen eighteen nineteen twenty."
    await chunk_lesson(chunks, lesson, **SMALL)

    stored = await _chunks(session, lesson)
    assert untouched in {c.id for c in stored}
    assert any("Echo seventeen" in c.content for c in stored)
    assert not any("Charlie eleven" in c.content for c in stored)


async def test_positions_follow_the_text_even_when_content_is_reused(
    session: AsyncSession,
) -> None:
    """A chunk's text can be unchanged while its place moves - inserting a
    paragraph above shifts everything below it. The row is reused, so its
    position has to be corrected rather than left stale."""
    lesson = await _lesson(session)
    chunks = ChunkRepository(session)
    await chunk_lesson(chunks, lesson, **SMALL)

    first_id = _only(await _chunks(session, lesson), FIRST).id

    lesson.content = f"Zulu nought nought nought.\n\n{BODY}"
    await chunk_lesson(chunks, lesson, **SMALL)

    stored = await _chunks(session, lesson)
    moved = _only(stored, FIRST)
    assert moved.id == first_id
    assert moved.position > 0
    assert [c.position for c in stored] == list(range(len(stored)))


async def test_emptying_a_lesson_removes_its_chunks(
    session: AsyncSession,
) -> None:
    """Otherwise the assistant would go on quoting text the lesson no longer
    contains."""
    lesson = await _lesson(session)
    chunks = ChunkRepository(session)
    await chunk_lesson(chunks, lesson, **SMALL)
    assert await _chunks(session, lesson)

    lesson.content = ""
    await chunk_lesson(chunks, lesson, **SMALL)

    assert await _chunks(session, lesson) == []


# --- the hash --------------------------------------------------------------


def test_the_same_text_always_hashes_the_same() -> None:
    """If it did not, every republish would look like a complete rewrite and
    FR-11a would save nothing."""
    assert content_hash("some text") == content_hash("some text")
    assert content_hash("some text") != content_hash("some text ")


# --- reading order ---------------------------------------------------------


async def test_lessons_come_back_in_reading_order(session: AsyncSession) -> None:
    """Publishing walks a course the way a student would, so a workflow
    history reads sensibly when something goes wrong half way through."""
    lesson = await _lesson(session)
    module = await session.get(Module, lesson.module_id)

    second = Module(course_id=module.course_id, title="Second", position=1)
    session.add(second)
    await session.flush()
    session.add_all(
        [
            Lesson(module_id=second.id, title="B", content="x", position=1),
            Lesson(module_id=second.id, title="A", content="x", position=0),
        ]
    )
    await session.flush()

    ids = await LessonRepository(session).ids_for_course(module.course_id)

    assert len(ids) == 3
    assert ids[0] == lesson.id


# --- which publish is this? ------------------------------------------------
#
# One decision, made once at the start of a publish and carried through the
# workflow: has this course been live before?
#
# A first publish runs on something nobody can see, so moving it through
# `publishing` costs nothing and FR-12 wants the visible progress. A
# republish runs on a course students are enrolled in and reading, and
# can_view only lets `ready` through - so the same status moves would take a
# live course out of the catalogue while a background job rebuilt data that
# no reader touches.


async def _course_of(session: AsyncSession, lesson: Lesson) -> Course:
    module = await session.get(Module, lesson.module_id)
    return await session.get(Course, module.course_id)


async def test_a_first_publish_shows_its_progress(session: AsyncSession) -> None:
    lesson = await _lesson(session)
    course = await _course_of(session, lesson)

    is_first = await begin_publish(CourseRepository(session), course_id=course.id)

    assert is_first is True
    assert course.status == "publishing"


async def test_retrying_a_failed_course_is_still_a_first_publish(
    session: AsyncSession,
) -> None:
    """`failed` means no publish ever succeeded, so nobody has seen it and
    there is nothing to protect."""
    lesson = await _lesson(session)
    course = await _course_of(session, lesson)
    course.status = "failed"
    await session.flush()

    is_first = await begin_publish(CourseRepository(session), course_id=course.id)

    assert is_first is True
    assert course.status == "publishing"


async def test_republishing_a_live_course_does_not_move_it(
    session: AsyncSession,
) -> None:
    """The change this whole path exists for.

    Students are reading this course. Its lesson text is untouched by a
    republish - only the chunks are rebuilt, and nothing outside publishing
    reads those until the embeddings arrive in Module 4. So the honest
    status for a course being republished is the one it already has.
    """
    lesson = await _lesson(session)
    course = await _course_of(session, lesson)
    course.status = "ready"
    await session.flush()

    is_first = await begin_publish(CourseRepository(session), course_id=course.id)

    assert is_first is False
    assert course.status == "ready"


async def test_going_live_records_when(session: AsyncSession) -> None:
    """published_at has existed since Module 1 and was never written, so
    every course has reported null for it. This is the moment it means
    something."""
    lesson = await _lesson(session)
    course = await _course_of(session, lesson)
    assert course.published_at is None
    at = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)

    await complete_publish(
        CourseRepository(session), course_id=course.id, finished_at=at
    )

    assert course.status == "ready"
    assert course.published_at == at


async def test_the_publication_date_is_not_rewritten_by_a_republish(
    session: AsyncSession,
) -> None:
    """It means "when this course went live", not "when it last changed" -
    updated_at from TimestampMixin already answers the second.

    A republish never calls complete_publish at all, so this holds by
    construction; the test pins it so a later refactor that calls it
    unconditionally does not quietly redate every course.
    """
    lesson = await _lesson(session)
    course = await _course_of(session, lesson)
    first = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    courses = CourseRepository(session)

    await complete_publish(courses, course_id=course.id, finished_at=first)
    is_first = await begin_publish(courses, course_id=course.id)

    assert is_first is False
    assert course.published_at == first
