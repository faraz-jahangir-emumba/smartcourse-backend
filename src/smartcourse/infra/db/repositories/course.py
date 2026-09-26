"""Queries against courses, modules and lessons.

Three classes in one file because they are one content tree - a course holds
modules, which hold lessons - and splitting them across three files would
separate things that are almost always read together.

Each is deliberately thin. A repository answers "fetch this", "store this";
it never decides whether an action is *allowed*. Ownership and state checks
live in the service, because they are business rules.
"""

from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smartcourse.infra.db.models.course import Course, Lesson, Module


class CourseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, course_id: UUID) -> Course | None:
        return await self._session.get(Course, course_id)

    async def get_with_contents(self, course_id: UUID) -> Course | None:
        """A course with its modules and their lessons.

        selectinload is what loads them. Without it, reading course.modules
        raises - the relationships use lazy="raise" so a query can never
        happen by accident. Here it is wanted, so it is asked for.

        Three queries, not one per module: SQLAlchemy fetches all the lessons
        for all the modules with a single IN clause. That is what avoids the
        N+1 problem, where listing 20 modules quietly costs 21 queries.
        """
        return await self._session.scalar(
            select(Course)
            .where(Course.id == course_id)
            .options(selectinload(Course.modules).selectinload(Module.lessons))
        )

    async def list(
        self,
        *,
        status: str | None = None,
        instructor_id: UUID | None = None,
        visible_statuses: tuple[str, ...] | None = None,
        also_owned_by: UUID | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Course], int]:
        """A page of courses, and the total matching the same filters.

        Two kinds of filter here, and the difference matters.

        `status` and `instructor_id` are what the *caller asked for* - the
        query string. `visible_statuses` and `also_owned_by` are what the
        caller is *allowed to see*, decided by the service. Keeping them
        separate means a request cannot widen its own visibility by passing
        a different status.

        Two queries on purpose. Counting and fetching a page are different
        questions, and folding them into one with a window function makes the
        query harder to read for no gain at this size.
        """
        filters = []
        if status is not None:
            filters.append(Course.status == status)
        if instructor_id is not None:
            filters.append(Course.instructor_id == instructor_id)

        if visible_statuses is not None:
            # "courses in these states, or ones I own" - so an instructor sees
            # their own drafts without seeing anyone else's.
            visibility = Course.status.in_(visible_statuses)
            if also_owned_by is not None:
                visibility = or_(visibility, Course.instructor_id == also_owned_by)
            filters.append(visibility)

        total = await self._session.scalar(
            select(func.count()).select_from(Course).where(*filters)
        )
        rows = await self._session.scalars(
            select(Course)
            .where(*filters)
            # A stable, total order. Without one, paging is unreliable:
            # Postgres may return rows differently between queries, so page 2
            # can repeat or skip rows from page 1. created_at alone is not
            # enough - two courses can share a timestamp - so id breaks ties.
            .order_by(Course.created_at.desc(), Course.id)
            .limit(limit)
            .offset(offset)
        )
        return list(rows), int(total or 0)

    async def add(self, course: Course) -> Course:
        self._session.add(course)
        await self._session.flush()
        return course

    async def delete(self, course: Course) -> None:
        """Remove a course, and everything beneath it.

        Modules, lessons and assets go with it - those foreign keys cascade,
        because a module without a course is unreachable. Enrollments do not:
        those RESTRICT, so Postgres refuses while anyone is enrolled rather
        than silently erasing learning history.
        """
        await self._session.delete(course)
        await self._session.flush()


class ModuleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, module_id: UUID) -> Module | None:
        return await self._session.get(Module, module_id)

    async def get_with_lessons(self, module_id: UUID) -> Module | None:
        """A module and its lessons, in order.

        selectinload for the same reason as the course read: the relationship
        is lazy="raise", so loading has to be asked for rather than happening
        by accident.
        """
        return await self._session.scalar(
            select(Module)
            .where(Module.id == module_id)
            .options(selectinload(Module.lessons))
        )

    async def list_for_course(self, course_id: UUID) -> list[Module]:
        rows = await self._session.scalars(
            select(Module)
            .where(Module.course_id == course_id)
            .order_by(Module.position)
        )
        return list(rows)

    async def next_position(self, course_id: UUID) -> int:
        """One past the current highest, so new modules append to the end.

        Position is never chosen by a caller - it is the outcome of adding or
        reordering.
        """
        highest = await self._session.scalar(
            select(func.max(Module.position)).where(Module.course_id == course_id)
        )
        return 0 if highest is None else int(highest) + 1

    async def add(self, module: Module) -> Module:
        self._session.add(module)
        await self._session.flush()
        return module

    async def delete(self, module: Module) -> None:
        await self._session.delete(module)
        await self._session.flush()

    async def flush(self) -> None:
        """Write pending position changes.

        Reordering mutates several modules at once. They are tracked objects,
        so changing .position is enough for SQLAlchemy to know - this just
        sends the UPDATEs now rather than at commit, so a violated constraint
        surfaces inside the service where it can be reported properly.
        """
        await self._session.flush()


class LessonRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, lesson_id: UUID) -> Lesson | None:
        return await self._session.get(Lesson, lesson_id)

    async def list_for_module(self, module_id: UUID) -> list[Lesson]:
        rows = await self._session.scalars(
            select(Lesson)
            .where(Lesson.module_id == module_id)
            .order_by(Lesson.position)
        )
        return list(rows)

    async def next_position(self, module_id: UUID) -> int:
        highest = await self._session.scalar(
            select(func.max(Lesson.position)).where(Lesson.module_id == module_id)
        )
        return 0 if highest is None else int(highest) + 1

    async def add(self, lesson: Lesson) -> Lesson:
        self._session.add(lesson)
        await self._session.flush()
        return lesson

    async def delete(self, lesson: Lesson) -> None:
        await self._session.delete(lesson)
        await self._session.flush()

    async def flush(self) -> None:
        await self._session.flush()
