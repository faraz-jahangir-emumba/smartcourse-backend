"""The work a publishing workflow does. FR-11, FR-12, FR-13.

Activities are the half of Temporal where anything is allowed - database
writes, network calls, the clock. The workflow that orchestrates them cannot
do any of that, because it is re-run from the top to rebuild its state and
must take the same path every time.

Each of these is a thin wrapper: open a session, call a function in
services/publishing.py, commit. The rules live there and know nothing about
Temporal, so they can also be called by a test, a command, or whatever
Module 3 brings.

Why each activity opens its own session rather than sharing one. There is no
request here, so nothing owns a transaction on their behalf - and an
activity is retried independently, possibly on a different worker after the
first one died. A session handed in from outside would belong to a process
that may no longer exist.
"""

from uuid import UUID

from temporalio import activity

from smartcourse.config import get_settings
from smartcourse.infra.db.models.course import (
    STATUS_FAILED,
    STATUS_PUBLISHING,
    STATUS_READY,
    Lesson,
)
from smartcourse.infra.db.repositories import (
    ChunkRepository,
    CourseRepository,
    LessonRepository,
)
from smartcourse.infra.db.session import SessionFactory
from smartcourse.services import publishing

# Ids cross the activity boundary as strings.
#
# Every argument and every return value is serialised to JSON and written
# into the workflow's history, and JSON has no UUID type. Converting at the
# edge keeps that explicit rather than depending on what the SDK's converter
# happens to support.


@activity.defn
async def mark_publishing(course_id: str) -> None:
    """FR-12's first half: the course is visibly being worked on."""
    async with SessionFactory() as session:
        await publishing.set_status(
            CourseRepository(session),
            course_id=UUID(course_id),
            status=STATUS_PUBLISHING,
        )
        await session.commit()


@activity.defn
async def list_lesson_ids(course_id: str) -> list[str]:
    """Every lesson in the course, in reading order.

    Read once, at the start, and the list travels in the workflow history
    from then on. So a lesson added half way through a publish is not picked
    up by that run - which is the right answer: a publish processes the
    course as it was when it started, and the next publish picks up the
    rest.
    """
    async with SessionFactory() as session:
        ids = await LessonRepository(session).ids_for_course(UUID(course_id))
        return [str(lesson_id) for lesson_id in ids]


@activity.defn
async def chunk_one_lesson(lesson_id: str) -> int:
    """Rebuild one lesson's chunks. Returns how many it now has.

    One activity per lesson, not one for the whole course. A failure then
    retries that lesson alone - the ones already done are recorded in the
    history and never run again, which is the durable execution the spike
    demonstrated. The cost is history size: 200 lessons is roughly 600
    events against a limit of 51,200.

    Idempotent, and it has to be: Temporal retries an activity whose worker
    died without knowing whether the work completed. Running this twice on
    the same lesson produces the same chunks, because the second run finds
    every hash already present and changes nothing.
    """
    settings = get_settings()

    async with SessionFactory() as session:
        lesson = await session.get(Lesson, UUID(lesson_id))
        if lesson is None:
            # Deleted between the listing and now. Not an error - there is
            # simply nothing to chunk, and failing the publish over it would
            # be worse than carrying on.
            return 0

        written = await publishing.chunk_lesson(
            ChunkRepository(session),
            lesson,
            max_tokens=settings.chunk_max_tokens,
            overlap_tokens=settings.chunk_overlap_tokens,
        )
        await session.commit()
        return written


@activity.defn
async def mark_ready(course_id: str) -> None:
    """FR-12: ready only once every piece of processing has finished.

    The workflow calls this last, after every lesson has been chunked. If
    any of them failed, the workflow raises instead and this never runs.
    """
    async with SessionFactory() as session:
        await publishing.set_status(
            CourseRepository(session),
            course_id=UUID(course_id),
            status=STATUS_READY,
        )
        await session.commit()


@activity.defn
async def mark_failed(course_id: str) -> None:
    """FR-13: the compensation.

    A course must never be left on `publishing` with nothing running. That
    is the state nobody can recover from by looking at it - it means either
    "still working" or "the worker died an hour ago" and there is no way to
    tell which.

    Note what this does *not* do: it does not delete the chunks written
    before the failure. A course that is not `ready` is invisible to
    students anyway, so nothing is half-published to anyone - and those
    chunks are keyed by content_hash, so the next attempt reuses them
    instead of redoing the work. Deleting them would throw away exactly what
    FR-11a exists to preserve.
    """
    async with SessionFactory() as session:
        await publishing.set_status(
            CourseRepository(session),
            course_id=UUID(course_id),
            status=STATUS_FAILED,
        )
        await session.commit()


# Registered with the worker as one list, so adding an activity here cannot
# be forgotten there. A worker that does not announce an activity leaves the
# workflow waiting for one that never comes - no error, just a workflow
# sitting on Running forever.
ALL_ACTIVITIES = [
    mark_publishing,
    list_lesson_ids,
    chunk_one_lesson,
    mark_ready,
    mark_failed,
]
