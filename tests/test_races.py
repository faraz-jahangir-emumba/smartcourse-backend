"""Check-then-write races. Review items #1 and #2.

These cannot use the `client` fixture. Every other test in the suite shares
one session wrapped in a transaction that is rolled back, which is what keeps
them independent - and it makes a race impossible to stage, because two
"concurrent" requests would be the same transaction and would see each
other's uncommitted work.

So these open real sessions, commit for real, and clean up by hand.
"""

import asyncio
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smartcourse.domain.errors import ConflictError
from smartcourse.infra.db.models.course import Course, Module
from smartcourse.infra.db.models.user import User
from smartcourse.infra.db.repositories import (
    CourseRepository,
    ModuleRepository,
    UserRepository,
)
from smartcourse.services import auth as auth_service
from smartcourse.services import course as course_service


async def test_two_modules_added_at_once_get_different_positions(engine) -> None:
    """next_position is MAX+1, which is a check-then-write.

    Without the lock both requests read the same highest position, both
    write the next one, and uq_modules_course_position rejects the loser -
    who did nothing wrong. With it, additions to one course serialise and
    the positions come out distinct.
    """
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]

    async with maker() as setup:
        teacher = User(
            email=f"race-mod-{tag}@example.com",
            password="x",
            full_name="Teacher",
            roles=["instructor"],
        )
        setup.add(teacher)
        await setup.flush()
        course = Course(
            instructor_id=teacher.id, title=f"Race {tag}", status="draft"
        )
        setup.add(course)
        await setup.commit()
        course_id, teacher_id = course.id, teacher.id

    async def add(title: str) -> str:
        async with maker() as s:
            user = await s.get(User, teacher_id)
            try:
                await course_service.add_module(
                    CourseRepository(s),
                    ModuleRepository(s),
                    course_id=course_id,
                    user=user,
                    title=title,
                )
                await s.commit()
                return "added"
            except Exception as exc:  # noqa: BLE001 - the point is what it was
                await s.rollback()
                return type(exc).__name__

    try:
        results = await asyncio.gather(add("First"), add("Second"))
        assert results == ["added", "added"], results

        async with maker() as check:
            rows = await check.scalars(
                select(Module.position)
                .where(Module.course_id == course_id)
                .order_by(Module.position)
            )
            assert list(rows) == [0, 1]
    finally:
        async with maker() as cleanup:
            await cleanup.execute(
                delete(Module).where(Module.course_id == course_id)
            )
            await cleanup.execute(delete(Course).where(Course.id == course_id))
            await cleanup.execute(delete(User).where(User.id == teacher_id))
            await cleanup.commit()


async def test_a_lost_duplicate_email_race_is_a_conflict_not_a_crash(
    engine,
) -> None:
    """register_user checks for the email first, which two requests can both
    pass before either writes. The unique index then rejects one of them.

    The check cannot be removed - it gives the good error in the ordinary
    case. What matters is that losing the race is reported as a conflict the
    caller can understand, not as an unhandled database error.
    """
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    email = f"race-dup-{uuid4().hex[:8]}@example.com"

    async def attempt() -> str:
        async with maker() as s:
            try:
                await auth_service.register_user(
                    UserRepository(s),
                    email=email,
                    password="correct-horse-battery",
                    full_name="Racer",
                    roles=["student"],
                )
                await s.commit()
                return "registered"
            except ConflictError:
                await s.rollback()
                return "conflict"
            except Exception as exc:  # noqa: BLE001
                await s.rollback()
                # Named so a failure says which exception escaped. Before the
                # IntegrityError handler this is what a caller got as a 500.
                return type(exc).__name__

    try:
        results = sorted(await asyncio.gather(attempt(), attempt()))

        # One wins. The other must fail in a way the API can turn into a 409 -
        # either the service check caught it, or IntegrityError did.
        assert results[1] == "registered", results
        assert results[0] in {"conflict", "IntegrityError"}, results

        async with maker() as check:
            rows = await check.scalars(
                select(User.id).where(User.email == email)
            )
            assert len(list(rows)) == 1
    finally:
        async with maker() as cleanup:
            await cleanup.execute(delete(User).where(User.email == email))
            await cleanup.commit()


async def test_integrity_errors_become_409_not_500(client) -> None:
    """The mapping itself, without needing a real race.

    IntegrityError is raised by Postgres when a unique constraint is
    violated. api/errors.py looks the constraint name up and turns a known
    one into a 409; anything unrecognised stays a 500, because an unexpected
    constraint failure is a bug rather than something the caller did.
    """
    from smartcourse.api.errors import _CONFLICT_BY_CONSTRAINT

    # Names verified against Postgres, not guessed:
    #   SELECT indexname FROM pg_indexes
    #    WHERE schemaname='public' AND indexdef LIKE 'CREATE UNIQUE%';
    assert "ix_users_email" in _CONFLICT_BY_CONSTRAINT
    assert "uq_modules_course_position" in _CONFLICT_BY_CONSTRAINT
    assert "uq_lessons_module_position" in _CONFLICT_BY_CONSTRAINT
