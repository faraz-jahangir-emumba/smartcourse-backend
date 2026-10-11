"""Enrollment use cases. FR-06 to FR-10, UC-02.

Three rules decide whether a student may join a course, and the order they
run in is a design decision rather than an accident - see enroll().

No queries here. The repository knows how to lock a row; this file knows why
it should be locked.
"""

from uuid import UUID

from smartcourse.domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from smartcourse.infra.db.models.course import Course
from smartcourse.infra.db.models.enrollment import Enrollment
from smartcourse.infra.db.models.user import User
from smartcourse.infra.db.repositories import CourseRepository, EnrollmentRepository


async def enroll(
    enrollments: EnrollmentRepository,
    courses: CourseRepository,
    *,
    course_id: UUID,
    user: User,
) -> Enrollment:
    """Join a course. FR-06, FR-07, FR-08, FR-09.

    The checks run cheapest-first, and the capacity check runs last on
    purpose. It is the only one that takes a lock, and a lock held across
    three other queries would serialise enrollments to a course for longer
    than the rule actually requires.
    """
    course = await courses.get_by_id(course_id)

    # 404 rather than "that course is not published yet". A student cannot
    # see drafts - _can_view in services/course.py already returns 404 for
    # them - and enrolling must agree, or it becomes a way to discover which
    # unpublished courses exist by trying to join them.
    if course is None or course.status != "ready":
        raise NotFoundError("No course with that id.")

    # UC-07: an instructor who wants to learn adds the student role to the
    # account they already have. That only means something if the role is
    # what grants this.
    if "student" not in user.roles:
        raise PermissionDeniedError(
            "Enrolling requires the student role.",
            details={"hint": "Add it with PATCH /users/me."},
        )

    # FR-06. The partial unique index enforces this too - the check is here
    # so the answer is a sensible message rather than a constraint violation
    # surfacing from the end of the request.
    existing = await enrollments.get_active(student_id=user.id, course_id=course_id)
    if existing is not None:
        raise ConflictError("You are already enrolled in this course.")

    await _assert_prerequisites_met(
        enrollments, courses, course_id=course_id, student_id=user.id
    )

    await _assert_seat_available(enrollments, course_id=course_id, course=course)

    enrollment = Enrollment(student_id=user.id, course_id=course_id, status="active")
    enrollments.add(enrollment)

    # Flush so a constraint violation surfaces here rather than at commit.
    # Two requests from the same student arriving together both pass the
    # get_active check above; the unique index is what actually stops the
    # second one, and this is where that becomes visible.
    await enrollments.flush()

    # FR-09 says enrolling creates a progress record. This row is it.
    #
    # It is tempting to also insert a lesson_progress row per lesson, marked
    # incomplete. That would be wrong: a lesson_progress row means "this
    # lesson was finished" - there is no done flag, only the row and a
    # timestamp. Zero rows is the correct representation of having started
    # nothing, and it keeps the percentage honest when a lesson is added
    # tomorrow.
    return enrollment


async def withdraw(
    enrollments: EnrollmentRepository,
    *,
    course_id: UUID,
    user: User,
) -> Enrollment:
    """Leave a course. FR-10.

    The row is kept and its status changed. Deleting it would lose history
    the analytics in FR-19 depend on, and would silently take the student's
    lesson progress with it - lesson_progress cascades from enrollments.
    """
    enrollment = await enrollments.get_active(
        student_id=user.id, course_id=course_id
    )
    if enrollment is None:
        raise NotFoundError("You are not enrolled in this course.")

    enrollment.status = "withdrawn"
    return enrollment


async def list_my_enrollments(
    enrollments: EnrollmentRepository,
    *,
    user: User,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Enrollment], int]:
    """Every course this student has joined, including the ones they left.

    History is returned, not filtered out. FR-10 keeps it so it can be read.
    """
    return await enrollments.list_for_student(user.id, limit=limit, offset=offset)


async def list_course_enrollments(
    enrollments: EnrollmentRepository,
    courses: CourseRepository,
    *,
    course_id: UUID,
    user: User,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Enrollment], int]:
    """The roster. The owning instructor, or an admin.

    Not the students. Who else is on a course is other people's business,
    and a class list is personal data.
    """
    course = await courses.get_by_id(course_id)
    if course is None:
        raise NotFoundError("No course with that id.")

    if "admin" not in user.roles and course.instructor_id != user.id:
        # 404, not 403. Consistent with the read rules in services/course.py:
        # confirming a course exists is itself information when the caller
        # has no business with it.
        raise NotFoundError("No course with that id.")

    return await enrollments.list_for_course(
        course_id, status=status, limit=limit, offset=offset
    )


async def _assert_prerequisites_met(
    enrollments: EnrollmentRepository,
    courses: CourseRepository,
    *,
    course_id: UUID,
    student_id: UUID,
) -> None:
    """FR-08. Every prerequisite must be *completed*, not merely started.

    Being half way through Python Basics is not knowing Python. An active
    enrollment satisfies nothing.
    """
    required = await courses.prerequisite_ids(course_id)
    if not required:
        return

    completed = await enrollments.completed_course_ids(student_id)
    missing = required - completed
    if not missing:
        return

    # Titles, not ids. "You still need 3f2a-..." is not something a student
    # can act on.
    titles = await courses.titles_for(missing)
    raise ConflictError(
        "You have not finished this course's prerequisites.",
        details={"missing": sorted(titles.values())},
    )


async def _assert_seat_available(
    enrollments: EnrollmentRepository,
    *,
    course_id: UUID,
    course: Course,
) -> None:
    """FR-07, and the only place in the project that takes an explicit lock.

    Null capacity means unlimited. There is nothing to race over, so an
    unlimited course takes no lock at all - the contention only exists where
    the constraint does.

    Where there is a capacity, the course row is locked before counting. Two
    requests arriving together would otherwise both count 49 against a limit
    of 50, both find room, and both insert. The lock makes the second one
    wait until the first has committed, so it counts 50 and is refused.

    The lock is held until the end of the request, which is also when the new
    enrollment is committed. That is what makes the count the second request
    sees include the first one.
    """
    if course.capacity is None:
        return

    locked = await enrollments.lock_course(course_id)
    if locked is None:
        raise NotFoundError("No course with that id.")

    taken = await enrollments.count_active(course_id)
    if taken >= locked.capacity:
        raise ConflictError(
            "This course is full.",
            details={"capacity": locked.capacity},
        )
