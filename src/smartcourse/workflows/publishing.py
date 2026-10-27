"""The publishing workflow. FR-12, FR-13.

This file decides *what happens in what order*. It does no work itself -
every line that touches the database is an activity, called through
workflow.execute_activity.

The constraint that shapes it: a workflow is re-run from the top whenever
Temporal needs to rebuild its state, after a crash or a deploy or simply
because it was evicted from memory. So it must take the same path every
time, which means no clock, no randomness and no I/O. Anything unpredictable
belongs in an activity, whose result is recorded once and replayed from the
history afterwards.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from smartcourse.workflows.activities import (
        begin_publish,
        chunk_one_lesson,
        list_lesson_ids,
        mark_failed,
        mark_ready,
    )

TASK_QUEUE = "smartcourse-publishing"


def workflow_id_for(course_id: str) -> str:
    """One publish per course at a time, enforced by Temporal rather than by
    a status check.

    Temporal refuses to start a workflow whose id matches one already
    running. So an instructor double-clicking Publish, or a client retrying
    a request it thought had failed, cannot start a second publish of the
    same course - not "is unlikely to", cannot.

    That is most of FR-17 for this flow, and it comes from choosing the id
    well rather than from writing locking code.
    """
    return f"publish-course-{course_id}"


@workflow.defn
class PublishCourseWorkflow:
    @workflow.run
    async def run(self, course_id: str) -> int:
        """Publish a course. Returns the number of chunks written.

        Two shapes, decided by the first activity:

            first publish   draft -> publishing -> ready
                                                -> failed
            republish       ready -> ready -> ready, whatever happens

        The chunking in the middle is identical. What differs is only
        whether the status column is allowed to move, and it is not allowed
        to for a course students are already reading - see begin_publish.
        """
        # Short timeout: this is a single UPDATE. A minute is already far
        # longer than it can legitimately take, and the point of the limit
        # is to notice a dead worker, not to accommodate a slow one.
        quick = timedelta(seconds=30)

        # Held in a local, which is safe precisely because it came from an
        # activity: the value is in the history and replays identically. A
        # workflow that branched on something it read itself would be a
        # different workflow on every replay.
        is_first = await workflow.execute_activity(
            begin_publish, course_id, start_to_close_timeout=quick
        )

        try:
            lesson_ids = await workflow.execute_activity(
                list_lesson_ids, course_id, start_to_close_timeout=quick
            )

            written = 0
            for lesson_id in lesson_ids:
                # One at a time rather than concurrently. 200 lessons at a
                # few milliseconds each is seconds, and sequential keeps the
                # history readable and the database load flat. Running them
                # with asyncio.gather works if that ever stops being true.
                written += await workflow.execute_activity(
                    chunk_one_lesson,
                    lesson_id,
                    start_to_close_timeout=timedelta(minutes=2),
                    # Three attempts, not the default of forever. Chunking
                    # is deterministic: if it fails on the same lesson three
                    # times it will fail the fourth, and retrying for ever
                    # would leave the course on `publishing` with no
                    # resolution and nobody told.
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )

            if is_first:
                await workflow.execute_activity(
                    mark_ready, course_id, start_to_close_timeout=quick
                )
            return written

        except Exception:
            # FR-13. The course must not be left on `publishing` - that
            # state means either "still working" or "the worker died an hour
            # ago", and nobody looking at it can tell which.
            #
            # This is the part Temporal makes reliable. An ordinary
            # try/except only runs if the process is alive to run it; here
            # the workflow's position is recorded, so if the worker is
            # killed between the failure and this line, Temporal resumes and
            # finishes the compensation anyway.
            #
            # A republish has nothing to compensate. The course never left
            # `ready`, and its chunks are still the ones from last time - a
            # complete, consistent set, just not the newest. Marking it
            # `failed` would unpublish a working course over derived data
            # no reader touches.
            if is_first:
                await workflow.execute_activity(
                    mark_failed, course_id, start_to_close_timeout=quick
                )
            # Re-raised so the workflow itself ends as failed. Swallowing it
            # would report success for a publish that did not happen - the
            # same mistake as committing after the response was sent.
            raise
