"""The whole machine at once: real workflow, real activities, real database.

Every other publishing test runs one half against a fake of the other.

    test_publishing.py        the service functions, real database,
                              no Temporal at all
    test_publish_workflow.py  the real workflow, real Temporal server,
                              activities replaced by fakes
    test_publish_endpoint.py  the real endpoint, Temporal client stubbed

Each is the right shape for what it tests, and together they still leave a
gap: nothing checks that the pieces fit. The seams between them are where
the untested things live - arguments crossing the activity boundary as
JSON, a workflow branching on a value an activity returned, a status
written by one activity and read by the next.

That gap was only ever closed by a spike script, which was deleted when the
spike was. This file is the replacement, and it is deliberately short: one
publish, one republish, and the assertions that neither can be faked.

Needs Postgres *and* Temporal. Skips with a clear message if Temporal is
not reachable, except in CI where one is started for the run.

Why this file does not use the `session` fixture
------------------------------------------------
Every other test runs inside a transaction that is rolled back, which is
what keeps them independent. That cannot work here. Activities open their
own sessions, on their own connections, precisely because they may be
retried on a different worker after the first one died - so they cannot see
uncommitted work, and nothing they commit would be inside our transaction
anyway.

So this test commits for real and cleans up after itself. The rows are
deleted in a finally, and courses cascade to modules, lessons and chunks.
"""

import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from temporalio.client import Client
from temporalio.worker import Worker

from smartcourse.config import get_settings
from smartcourse.infra.db.models.content import Chunk
from smartcourse.infra.db.models.course import Course, Lesson, Module
from smartcourse.infra.db.models.user import User
from smartcourse.workflows import activities as activities_module
from smartcourse.workflows.activities import ALL_ACTIVITIES
from smartcourse.workflows.publishing import PublishCourseWorkflow, workflow_id_for

TEMPORAL_ADDRESS = "localhost:7233"

FIRST = "A commit is a snapshot of the whole project, not a diff."
SECOND = "A branch is a movable label pointing at one commit."
THIRD = "Merging replays one branch's commits onto another."


@pytest.fixture(scope="module")
async def temporal_client():
    """A client, or a skip. Same reasoning as test_publish_workflow.py:
    failing when no server is running would be indistinguishable from the
    workflow being broken."""
    try:
        return await Client.connect(TEMPORAL_ADDRESS)
    except Exception as exc:  # noqa: BLE001 - any connection problem counts
        if os.environ.get("CI"):
            raise
        pytest.skip(f"no Temporal at {TEMPORAL_ADDRESS}: {exc}")


@pytest.fixture
async def live(test_database: None, monkeypatch: pytest.MonkeyPatch):
    """A session factory that really commits, and the activities pointed at it.

    conftest sets POSTGRES_DB before anything imports smartcourse, so the
    SessionFactory the activities already use is bound to the test database
    - the patch is not about *which* database.

    It is about the event loop. That factory's engine is module-level and
    pools its connections, and an asyncpg connection belongs to the loop
    that opened it; pytest-asyncio gives each test a new one. The same
    reason conftest builds a fresh NullPool engine per test, for the same
    fix.
    """
    engine = create_async_engine(
        get_settings().database_url, echo=False, poolclass=NullPool
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(activities_module, "SessionFactory", factory)

    yield factory

    await engine.dispose()


async def _course_with_lessons(factory) -> tuple[Course, list[Lesson]]:
    """Three short lessons, committed so another connection can see them."""
    tag = uuid.uuid4().hex[:8]
    async with factory() as session:
        teacher = User(
            email=f"e2e-{tag}@example.com",
            password="x",
            full_name="Teacher",
            roles=["instructor"],
        )
        session.add(teacher)
        await session.flush()

        course = Course(
            instructor_id=teacher.id, title=f"E2E {tag}", status="draft"
        )
        session.add(course)
        await session.flush()

        module = Module(course_id=course.id, title="Module", position=0)
        session.add(module)
        await session.flush()

        lessons = [
            Lesson(module_id=module.id, title=f"L{i}", content=text, position=i)
            for i, text in enumerate((FIRST, SECOND, THIRD))
        ]
        session.add_all(lessons)
        await session.commit()
        return course, lessons


async def _delete(factory, course: Course) -> None:
    """Courses cascade to modules, lessons and chunks. The instructor is
    RESTRICT, so it goes after."""
    async with factory() as session:
        fresh = await session.get(Course, course.id)
        if fresh is not None:
            instructor_id = fresh.instructor_id
            await session.delete(fresh)
            await session.flush()
            teacher = await session.get(User, instructor_id)
            if teacher is not None:
                await session.delete(teacher)
        await session.commit()


async def _publish(client: Client, course_id) -> int:
    """Run one real publish: a worker announcing the real activities.

    A task queue per call, so a worker left over from another test cannot
    pick up this one's work.
    """
    queue = f"e2e-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[PublishCourseWorkflow],
        activities=ALL_ACTIVITIES,
    ):
        return await client.execute_workflow(
            PublishCourseWorkflow.run,
            str(course_id),
            id=f"{workflow_id_for(str(course_id))}-{queue}",
            task_queue=queue,
        )


async def _chunks(factory, course: Course) -> dict[str, tuple]:
    """Every chunk of the course, keyed by lesson title: (id, content)."""
    async with factory() as session:
        rows = (
            await session.execute(
                select(Lesson.title, Chunk.id, Chunk.content)
                .join(Chunk, Chunk.lesson_id == Lesson.id)
                .join(Module, Module.id == Lesson.module_id)
                .where(Module.course_id == course.id)
                .order_by(Lesson.position, Chunk.position)
            )
        ).all()
    return {title: (chunk_id, content) for title, chunk_id, content in rows}


async def test_publishing_and_republishing_a_real_course(
    live, temporal_client: Client
) -> None:
    """One publish, one republish, against everything at once.

    The assertions are chosen to be ones the component tests cannot make.

    That `is_first` survives the round trip is the newest and least proven
    part: it is produced by an activity, serialised into the workflow's
    history as JSON, and read back as the condition of an `if`. Both
    branches are covered by fakes elsewhere; this is the only place the real
    value crosses the boundary.

    And the chunk ids are the proof of FR-11a end to end. test_publishing.py
    makes the same assertion about chunk_lesson in isolation - here the
    lessons are selected by one activity, chunked by another, and the ids
    are read back from the database afterwards.
    """
    course, lessons = await _course_with_lessons(live)

    try:
        # --- first publish ------------------------------------------------
        written = await _publish(temporal_client, course.id)
        assert written == 3, "one chunk per short lesson"

        async with live() as session:
            fresh = await session.get(Course, course.id)
            assert fresh.status == "ready"
            assert fresh.published_at is not None
            went_live = fresh.published_at

        before = await _chunks(live, course)
        assert set(before) == {"L0", "L1", "L2"}
        assert before["L1"][1] == SECOND

        # --- edit one lesson ----------------------------------------------
        async with live() as session:
            edited = await session.get(Lesson, lessons[1].id)
            edited.content = "A branch is a label. Creating one copies nothing."
            await session.commit()

        # --- republish ----------------------------------------------------
        # 202 in the API; here the workflow runs to completion again. The
        # status check that used to refuse this is gone.
        written = await _publish(temporal_client, course.id)
        assert written == 3

        async with live() as session:
            fresh = await session.get(Course, course.id)
            # Never moved. A live course stays in the catalogue while its
            # chunks are rebuilt - the status column is what can_view
            # consults, and nothing a reader can see has changed.
            assert fresh.status == "ready"
            # Not redated: published_at means when this course went live.
            assert fresh.published_at == went_live

        after = await _chunks(live, course)

        # FR-11a, end to end. The two untouched lessons keep their rows -
        # same id means the row was never replaced, which in Module 4 means
        # the embedding was not thrown away and bought again.
        assert after["L0"][0] == before["L0"][0]
        assert after["L2"][0] == before["L2"][0]

        # The edited one did not.
        assert after["L1"][0] != before["L1"][0]
        assert "copies nothing" in after["L1"][1]

    finally:
        await _delete(live, course)
