"""The publishing workflow itself. FR-12, FR-13.

These test *orchestration* - what the workflow decides, and what it does
when something fails. Almost all of them replace the real activities with
fakes, which is the usual way to test a Temporal workflow: the workflow code
is real, the work it drives is not, and no database is involved.

That split matters. tests/test_publishing.py already covers what the
activities do. What is untested until here is the thing Temporal exists for:
that a failure half way through still ends with the course marked `failed`
rather than stranded on `publishing`.

Needs a Temporal server. These skip with a clear message if one is not
reachable, and CI starts one so they always run there.
"""

import os
import uuid

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.worker import Worker

from smartcourse.workflows.publishing import PublishCourseWorkflow, workflow_id_for

TEMPORAL_ADDRESS = "localhost:7233"


@pytest.fixture(scope="module")
async def temporal_client():
    """A client, or a skip.

    Connecting is the test: if Temporal is not running there is nothing
    useful to say, and failing would be indistinguishable from the workflow
    being broken.
    """
    try:
        return await Client.connect(TEMPORAL_ADDRESS)
    except Exception as exc:  # noqa: BLE001 - any connection problem counts
        if os.environ.get("CI"):
            # Not in CI. There Temporal is started by the workflow, so an
            # unreachable server is a broken pipeline - and a skipped test
            # in CI looks exactly like a passing one, which would leave
            # FR-13's compensation unproven while the build stayed green.
            raise
        pytest.skip(f"no Temporal at {TEMPORAL_ADDRESS}: {exc}")


class Recorder:
    """Stand-ins for the five activities, remembering what was called.

    Each has the same registered name as the real one, which is all Temporal
    matches on - the workflow asks for "chunk_one_lesson" by name and gets
    whichever function the worker registered under it.
    """

    def __init__(
        self,
        *,
        lessons: int = 3,
        fail_on: int | None = None,
        is_first: bool = True,
    ) -> None:
        self.calls: list[str] = []
        self.lessons = lessons
        self.fail_on = fail_on
        self.chunk_attempts = 0
        # What begin_publish reports back. True is a first publish, False a
        # republish of a course that is already live.
        self.is_first = is_first

    def activities(self) -> list:
        recorder = self

        @activity.defn(name="begin_publish")
        async def begin_publish(course_id: str) -> bool:
            recorder.calls.append("begin_publish")
            return recorder.is_first

        @activity.defn(name="list_lesson_ids")
        async def list_lesson_ids(course_id: str) -> list[str]:
            recorder.calls.append("list_lesson_ids")
            return [str(uuid.uuid4()) for _ in range(recorder.lessons)]

        @activity.defn(name="chunk_one_lesson")
        async def chunk_one_lesson(lesson_id: str) -> int:
            recorder.chunk_attempts += 1
            recorder.calls.append("chunk_one_lesson")
            if (
                recorder.fail_on is not None
                and recorder.chunk_attempts >= recorder.fail_on
            ):
                raise RuntimeError("chunking blew up")
            return 2

        @activity.defn(name="mark_ready")
        async def mark_ready(course_id: str) -> None:
            recorder.calls.append("mark_ready")

        @activity.defn(name="mark_failed")
        async def mark_failed(course_id: str) -> None:
            recorder.calls.append("mark_failed")

        return [
            begin_publish,
            list_lesson_ids,
            chunk_one_lesson,
            mark_ready,
            mark_failed,
        ]


async def _run(client: Client, recorder: Recorder, course_id: str):
    """Start a worker on a queue of its own and run one workflow on it.

    A fresh queue per test, so a worker left over from another test cannot
    pick up this one's work - the task queue is the only thing connecting
    them, and reusing a name would make the tests depend on each other.
    """
    queue = f"test-{uuid.uuid4().hex[:8]}"

    async with Worker(
        client,
        task_queue=queue,
        workflows=[PublishCourseWorkflow],
        activities=recorder.activities(),
    ):
        return await client.execute_workflow(
            PublishCourseWorkflow.run,
            course_id,
            id=f"{workflow_id_for(course_id)}-{queue}",
            task_queue=queue,
        )


# --- the happy path --------------------------------------------------------


async def test_it_marks_publishing_then_chunks_then_marks_ready(
    temporal_client: Client,
) -> None:
    """FR-12. The order is the contract: a course is only `ready` once every
    lesson has been processed."""
    recorder = Recorder(lessons=3)

    written = await _run(temporal_client, recorder, str(uuid.uuid4()))

    assert recorder.calls == [
        "begin_publish",
        "list_lesson_ids",
        "chunk_one_lesson",
        "chunk_one_lesson",
        "chunk_one_lesson",
        "mark_ready",
    ]
    assert written == 6  # three lessons, two chunks each
    assert "mark_failed" not in recorder.calls


async def test_a_course_with_no_lessons_still_becomes_ready(
    temporal_client: Client,
) -> None:
    """An empty course is publishable. There is nothing to chunk, which is
    not a failure - and refusing would mean an instructor cannot publish a
    course skeleton before filling it in."""
    recorder = Recorder(lessons=0)

    written = await _run(temporal_client, recorder, str(uuid.uuid4()))

    assert written == 0
    assert recorder.calls == ["begin_publish", "list_lesson_ids", "mark_ready"]


# --- FR-13, the reason Temporal is in the brief ----------------------------


async def test_a_failure_marks_the_course_failed_and_not_ready(
    temporal_client: Client,
) -> None:
    """The requirement this module exists to teach.

    A course left on `publishing` is the one state nobody can recover from
    by looking at it: it means either "still working" or "the worker died an
    hour ago", and there is no way to tell which. So a failure must end
    somewhere final.
    """
    recorder = Recorder(lessons=3, fail_on=2)

    with pytest.raises(WorkflowFailureError):
        await _run(temporal_client, recorder, str(uuid.uuid4()))

    assert "mark_failed" in recorder.calls
    assert "mark_ready" not in recorder.calls
    # The compensation runs last, after the failure, not instead of it.
    assert recorder.calls[-1] == "mark_failed"


async def test_the_workflow_itself_fails_rather_than_reporting_success(
    temporal_client: Client,
) -> None:
    """The compensation does not swallow the error.

    Catching it and returning normally would report a successful publish
    that did not happen - the same mistake as committing after the response
    has gone out, which the Week 1 review caught in get_session.
    """
    recorder = Recorder(lessons=1, fail_on=1)

    with pytest.raises(WorkflowFailureError):
        await _run(temporal_client, recorder, str(uuid.uuid4()))


async def test_a_failed_lesson_is_retried_before_giving_up(
    temporal_client: Client,
) -> None:
    """Three attempts, not Temporal's default of forever.

    Forever suits transient failures, and chunking is deterministic - the
    same lesson fails the same way. Worse, a workflow retrying for ever
    never reaches its except block, so the compensation never runs and the
    course stays on `publishing` with nobody told. Infinite retries would
    defeat FR-13.
    """
    recorder = Recorder(lessons=1, fail_on=1)

    with pytest.raises(WorkflowFailureError):
        await _run(temporal_client, recorder, str(uuid.uuid4()))

    assert recorder.chunk_attempts == 3


# --- FR-17, one publish at a time ------------------------------------------


async def test_the_same_course_cannot_be_published_twice_at_once(
    temporal_client: Client,
) -> None:
    """Temporal refuses a second workflow with an id already running.

    This is what actually stops a double-click starting two publishes - not
    the status check in the endpoint, which two simultaneous requests would
    both pass before either wrote anything.
    """
    from temporalio.exceptions import WorkflowAlreadyStartedError

    course_id = str(uuid.uuid4())
    workflow_id = workflow_id_for(course_id)
    queue = f"test-{uuid.uuid4().hex[:8]}"

    # No worker on this queue, so the first workflow starts and then waits
    # for one that never arrives - which is exactly the window a second
    # request would land in.
    handle = await temporal_client.start_workflow(
        PublishCourseWorkflow.run, course_id, id=workflow_id, task_queue=queue
    )
    try:
        with pytest.raises(WorkflowAlreadyStartedError):
            await temporal_client.start_workflow(
                PublishCourseWorkflow.run,
                course_id,
                id=workflow_id,
                task_queue=queue,
            )
    finally:
        await handle.terminate()


# --- republishing ----------------------------------------------------------
#
# The same chunking on a course that is already live. What changes is that
# the workflow must not touch the status: `can_view` only lets `ready`
# through, so moving it would take a published course out of the catalogue
# for the length of a background job - and leave it out for good if the job
# then failed.


async def test_a_republish_chunks_without_touching_the_status(
    temporal_client: Client,
) -> None:
    """Every lesson is still processed. Neither status activity runs."""
    recorder = Recorder(lessons=3, is_first=False)

    written = await _run(temporal_client, recorder, str(uuid.uuid4()))

    assert written == 6
    assert recorder.calls == [
        "begin_publish",
        "list_lesson_ids",
        "chunk_one_lesson",
        "chunk_one_lesson",
        "chunk_one_lesson",
    ]
    assert "mark_ready" not in recorder.calls


async def test_a_failed_republish_leaves_the_course_alone(
    temporal_client: Client,
) -> None:
    """The asymmetry that makes republishing safe.

    A first publish that fails goes to `failed`, and should: nobody could
    see the course anyway. A republish that fails must not, because the
    course is live, enrolled students are reading it, and `failed` is both
    terminal and invisible. The material in lessons.content is untouched by
    any of this - only the chunks are stale, and nothing reads those until
    Module 4.

    The cost is that nobody is told. GET /courses/{id}/publish/status is
    where that answer lives; recording it durably is Module 3's FailedEvent.
    """
    recorder = Recorder(lessons=3, fail_on=1, is_first=False)

    with pytest.raises(WorkflowFailureError):
        await _run(temporal_client, recorder, str(uuid.uuid4()))

    assert "mark_failed" not in recorder.calls
    assert "mark_ready" not in recorder.calls


async def test_the_workflow_still_fails_so_temporal_records_it(
    temporal_client: Client,
) -> None:
    """Leaving the course alone is not the same as reporting success.

    The workflow re-raises, so Temporal marks the execution Failed and the
    history keeps the exception and the lesson it died on. That recorded
    outcome is the only trace a failed republish leaves - and it is exactly
    what the publish-status endpoint reads back.
    """
    recorder = Recorder(lessons=2, fail_on=1, is_first=False)

    with pytest.raises(WorkflowFailureError):
        await _run(temporal_client, recorder, str(uuid.uuid4()))
