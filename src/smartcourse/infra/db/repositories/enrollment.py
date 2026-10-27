"""Queries against enrollments.

Thin, like the others. Nothing here decides whether somebody *may* enrol -
that is FR-06, FR-07 and FR-08, and they live in the service. This file only
knows how to ask Postgres questions.
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse.infra.db.models.course import Course
from smartcourse.infra.db.models.enrollment import ENROLLMENT_ACTIVE, Enrollment


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
