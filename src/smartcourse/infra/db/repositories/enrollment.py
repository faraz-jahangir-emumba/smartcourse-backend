"""Queries against enrollments.

Thin, like the others. Nothing here decides whether somebody *may* enrol -
that is FR-06, FR-07 and FR-08, and they live in the service. This file only
knows how to ask Postgres questions.
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse.infra.db.models.course import Course
from smartcourse.infra.db.models.enrollment import (
    ENROLLMENT_ACTIVE,
    ENROLLMENT_COMPLETED,
    Enrollment,
    LessonProgress,
)


class EnrollmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_course(self, course_id: UUID) -> Course | None:
        """Fetch a course and hold a lock on its row until the request ends.

        This is the whole answer to the capacity race. Without it, two
        requests both count 49 against a capacity of 50, both decide there is
        room, and both insert - because neither had committed when the other
        looked.

        FOR UPDATE makes the second request wait at this line until the first
        commits. It then counts again and sees 50. It is not refused by the
        lock; it is simply made to read the truth.

        Note this is a select() rather than session.get(). get() can answer
        from SQLAlchemy's identity map when the object is already loaded -
        which it will be, because the service checks visibility first. That
        would return the course without ever sending SQL, and a lock you
        never asked the database for is not a lock.

        Only called when the course has a capacity. Null means unlimited,
        there is nothing to race over, and an unlimited course should not
        serialise its enrollments for no reason.
        """
        return await self._session.scalar(
            select(Course).where(Course.id == course_id).with_for_update()
        )

    async def count_active(self, course_id: UUID) -> int:
        """How many people are currently enrolled.

        Active only. Withdrawn is obvious - somebody who left is not taking
        up a place.

        Completed is the interesting one, and it is deliberate. Capacity
        means how many people can be studying at once, not how many places
        exist for all time: a course with capacity 2 whose two students
        finish would otherwise be permanently full and could never be
        taught again. The course is a reusable asset, not a one-off cohort.

        The consequence to be aware of: this is not a popularity figure. A
        course five hundred people have completed reports 0. "How many have
        ever taken this" is a different question and would need a different
        count.
        """
        return await self._session.scalar(
            select(func.count())
            .select_from(Enrollment)
            .where(
                Enrollment.course_id == course_id,
                Enrollment.status == ENROLLMENT_ACTIVE,
            )
        )

    async def get_active(
        self, *, student_id: UUID, course_id: UUID
    ) -> Enrollment | None:
        """This student's live enrollment in this course, if any.

        At most one can exist - uq_enrollments_one_active is a unique index
        over exactly this pair, limited to active rows.
        """
        return await self._session.scalar(
            select(Enrollment).where(
                Enrollment.student_id == student_id,
                Enrollment.course_id == course_id,
                Enrollment.status == ENROLLMENT_ACTIVE,
            )
        )

    async def get_for_content(
        self, *, student_id: UUID, course_id: UUID
    ) -> Enrollment | None:
        """This student's enrollment, if it still entitles them to the
        material. Active or completed - not withdrawn.

        Deliberately not `get_active`, and deliberately not a flag on it.
        They answer different questions and the difference matters:

            get_active        may they take up a *seat*?  Active only.
            get_for_content   may they read the *material*?  Also completed.

        `get_active` is what the capacity check uses, and capacity is about
        how many people can study at once - a student who has finished is
        not studying and their seat goes back to the pool. Reading is a
        different question: they earned the material by finishing it.

        Withdrawing is the opposite case and stays excluded. Keeping the
        content after choosing to leave would make withdrawing free, which
        is the rule the gate was written for.

        Without this, FR-15 would lock a student out of a course at the
        exact moment they completed it - `may_read_content` asked
        `get_active`, written in Module 2 when `completed` was unreachable
        and active was the only state that occurred.
        """
        return await self._session.scalar(
            select(Enrollment).where(
                Enrollment.student_id == student_id,
                Enrollment.course_id == course_id,
                Enrollment.status.in_((ENROLLMENT_ACTIVE, ENROLLMENT_COMPLETED)),
            )
        )

    async def mark_lesson_complete(
        self, *, enrollment_id: UUID, lesson_id: UUID
    ) -> bool:
        """Record that a lesson is finished. True if this call is what did it.

        FR-14, and FR-17's "running twice does not double-count" in its
        smallest form. A student double-clicking, or a phone retrying a
        request whose response was lost, must not produce two rows - the
        percentage would exceed 100.

        ON CONFLICT DO NOTHING, rather than SELECT-then-INSERT. The second
        is a check-then-act: both requests look, both find nothing, both
        insert, and the loser gets an IntegrityError from
        uq_lesson_progress_once at some unhelpful later moment. Postgres
        settles it inside the single statement instead.

        Worth comparing with the capacity check above, which solves a race
        with SELECT ... FOR UPDATE. Different problem, different answer.
        There, the decision depends on *counting* other rows, so the rows
        have to be held still while we count. Here nothing is counted - the
        row either exists or it does not, and a unique index already knows
        which. A lock would be a heavier tool answering a question nobody
        asked.

        RETURNING tells us which happened: a row back means this call
        inserted it, nothing back means it was already there. The caller
        needs that to know whether anything actually changed.
        """
        statement = (
            pg_insert(LessonProgress)
            .values(enrollment_id=enrollment_id, lesson_id=lesson_id)
            .on_conflict_do_nothing(constraint="uq_lesson_progress_once")
            .returning(LessonProgress.id)
        )
        return await self._session.scalar(statement) is not None

    async def completed_lesson_ids(self, enrollment_id: UUID) -> set[UUID]:
        """Which lessons this enrollment has finished.

        A set, because every question asked of it is membership or size:
        "is this lesson done", "how many are done", and - for FR-15 -
        "is every lesson in the course in here".

        Ids rather than rows. Nothing needs `completed_at` yet, and
        fetching whole rows to count them is the habit that turns a
        progress bar into a table scan.
        """
        rows = await self._session.scalars(
            select(LessonProgress.lesson_id).where(
                LessonProgress.enrollment_id == enrollment_id
            )
        )
        return set(rows)

    async def completed_course_ids(self, student_id: UUID) -> set[UUID]:
        """Every course this student has finished. Used for prerequisites.

        A set because the only question asked of it is membership, and
        because a student who enrolled, withdrew and returned has two rows
        for one course.

        Only 'completed' counts. An active enrollment means they are part way
        through, and half of a prerequisite is not a prerequisite.
        """
        rows = await self._session.scalars(
            select(Enrollment.course_id).where(
                Enrollment.student_id == student_id,
                Enrollment.status == "completed",
            )
        )
        return set(rows)

    async def list_for_student(
        self, student_id: UUID, *, limit: int = 20, offset: int = 0
    ) -> tuple[list[Enrollment], int]:
        """Everything this student has ever enrolled in, newest first.

        All statuses, not just active. FR-10 keeps history so it can be read;
        hiding it here would make the requirement pointless.
        """
        where = Enrollment.student_id == student_id

        total = await self._session.scalar(
            select(func.count()).select_from(Enrollment).where(where)
        )
        rows = await self._session.scalars(
            select(Enrollment)
            .where(where)
            .order_by(Enrollment.enrolled_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows), total

    async def list_for_course(
        self,
        course_id: UUID,
        *,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Enrollment], int]:
        """The roster. Defaults to every status so an instructor can see who
        dropped out, which is the more interesting question."""
        filters = [Enrollment.course_id == course_id]
        if status is not None:
            filters.append(Enrollment.status == status)

        total = await self._session.scalar(
            select(func.count()).select_from(Enrollment).where(*filters)
        )
        rows = await self._session.scalars(
            select(Enrollment)
            .where(*filters)
            .order_by(Enrollment.enrolled_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows), total

    def add(self, enrollment: Enrollment) -> None:
        self._session.add(enrollment)

    async def flush(self) -> None:
        """Send pending changes to Postgres without committing.

        Needed so a constraint violation surfaces here, inside the service,
        rather than at the end of the request where there is no longer any
        context to turn it into a sensible error.
        """
        await self._session.flush()
