"""Publishing a course. FR-11, FR-11a, FR-12, FR-13.

Ordinary async functions. Nothing here imports Temporal, and nothing here
knows it is being orchestrated - the Temporal activities in workflows/ are
thin wrappers that open a session and call these.

That separation is the point. The same steps could be driven by a Celery
task, a management command or a test, and the rules would be identical. It
also means publishing can be tested without a worker running.
"""

import hashlib
from uuid import UUID

from smartcourse.domain.errors import ConflictError, NotFoundError, PermissionDeniedError
from smartcourse.infra.db.models.content import Chunk
from smartcourse.infra.db.models.course import Course, Lesson
from smartcourse.infra.db.models.user import (
    ROLE_ADMIN,
    ROLE_INSTRUCTOR,
    User,
)
from smartcourse.infra.db.repositories import ChunkRepository, CourseRepository
from smartcourse.services.chunking import chunk_text, estimate_tokens
from smartcourse.services.course import can_view
from smartcourse.infra.db.models.course import STATUS_DRAFT, STATUS_FAILED

# Only these can be published. `publishing` is excluded so a second publish
# cannot start while one is running - though Temporal's workflow id is what
# actually enforces that; this is the readable error.
PUBLISHABLE_STATUSES = (STATUS_DRAFT, STATUS_FAILED)


def content_hash(text: str) -> str:
    """A fingerprint of a chunk's text.

    sha256 because it is in the standard library and collisions are not a
    practical concern. The point is not security - it is that identical text
    gives an identical hash, so a republish can tell which chunks actually
    changed and leave the rest alone. FR-11a.

    Why that matters later: in Module 4 each chunk carries an embedding that
    costs time and money to produce. Editing one lesson must not re-embed the
    other 199.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def assert_can_publish(
    courses: CourseRepository, *, course_id: UUID, user: User
) -> Course:
    """The checks the endpoint runs before starting a workflow.

    Deliberately separate from the workflow itself. A permission check
    belongs in the request that made it, where there is a caller to refuse -
    not in a background job that has already been accepted.
    """
    course = await courses.get_by_id(course_id)
    if course is None:
        raise NotFoundError("No course with that id.")

    # Which refusal depends on whether they can see the course at all, the
    # same way editing decides it:
    #
    #     cannot see it (someone else's draft)  -> 404, same as a read
    #     can see it but may not publish it     -> 403
    #
    # The first version returned 404 for every non-owner, which meant a
    # student looking at a published course in the catalogue was told it
    # did not exist. That contradicts the screen they came from, and it is
    # exactly the inconsistency the Week 1 review removed from editing.
    if not can_view(course, user):
        raise NotFoundError("No course with that id.")

    if ROLE_ADMIN not in user.roles:
        # Role first, then ownership. For a student, "publishing requires
        # the instructor role" is the useful answer; "this belongs to
        # another instructor" would be true and beside the point.
        if ROLE_INSTRUCTOR not in user.roles:
            raise PermissionDeniedError("Publishing requires the instructor role.")
        if course.instructor_id != user.id:
            raise PermissionDeniedError(
                "This course belongs to another instructor."
            )

    if course.status not in PUBLISHABLE_STATUSES:
        raise ConflictError(
            f"A course in '{course.status}' cannot be published.",
            details={"publishable_from": list(PUBLISHABLE_STATUSES)},
        )

    return course


async def set_status(
    courses: CourseRepository, *, course_id: UUID, status: str
) -> None:
    """Move a course to a new status.

    Called by the workflow rather than by the endpoint, and that is a
    decision worth remembering: Postgres and Temporal share no transaction,
    so an endpoint that wrote the status *and* started a workflow could do
    one and not the other. Letting the workflow own every write leaves one
    path and nothing half-done. The outbox pattern in Module 3 is the proper
    fix for the general case.
    """
    course = await courses.get_by_id(course_id)
    if course is None:
        raise NotFoundError("No course with that id.")
    course.status = status


async def chunk_lesson(
    chunks: ChunkRepository,
    lesson: Lesson,
    *,
    max_tokens: int,
    overlap_tokens: int,
) -> int:
    """Rebuild one lesson's chunks. Returns how many it now has.

    Incremental, which is the whole of FR-11a. The naive version - delete
    every chunk and insert the new ones - would be three lines shorter and
    would throw away every embedding on every republish, including for the
    199 lessons nobody touched.

    So: hash the new chunks, compare against what is stored, and only write
    the difference.

        same hash         keep the row, update its position if it moved
        new hash          insert
        hash gone         delete

    Position is updated rather than ignored because a chunk's text can be
    unchanged while its place in the lesson moves - inserting a paragraph
    above it shifts everything below.
    """
    existing = await chunks.existing_for_lesson(lesson.id)
    pieces = chunk_text(
        lesson.content, max_tokens=max_tokens, overlap_tokens=overlap_tokens
    )

    if not pieces:
        # A lesson emptied of content keeps no chunks. Leaving them would
        # have the assistant quoting text the lesson no longer contains.
        await chunks.delete_for_lesson(lesson.id)
        await chunks.flush()
        return 0

    by_hash = {chunk.content_hash: chunk for chunk in existing}
    kept: set[str] = set()

    for position, text in enumerate(pieces):
        digest = content_hash(text)
        reused = by_hash.get(digest)

        if reused is not None and digest not in kept:
            reused.position = position
            kept.add(digest)
            continue

        chunks.add(
            Chunk(
                lesson_id=lesson.id,
                # Null means the text came from lessons.content rather than
                # from an uploaded file. Assets arrive in Module 3.
                asset_id=None,
                content=text,
                position=position,
                token_count=estimate_tokens(text),
                content_hash=digest,
            )
        )
        kept.add(digest)

    stale = [chunk for chunk in existing if chunk.content_hash not in kept]
    if stale:
        await chunks.delete_many(stale)

    # Flush so a constraint violation surfaces here, inside the activity that
    # caused it, rather than at commit where there is no context left.
    await chunks.flush()
    return len(pieces)
