"""Studying a course. FR-14, FR-15, UC-03.

Separate from enrollment.py, which is about *admission* - whether a student
may join at all, and the three rules that decide it. This file is about what
happens afterwards, and the only moment the two meet is FR-15: finishing the
last lesson changes the enrollment's status.

No queries here. The repository knows how to insert a row without
duplicating it; this file knows when there is one to insert.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from smartcourse import events
from smartcourse.domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from smartcourse.infra.db.models.enrollment import (
    ENROLLMENT_COMPLETED,
    Enrollment,
)
from smartcourse.infra.db.models.user import User
from smartcourse.infra.db.repositories import (
    CourseRepository,
    EnrollmentRepository,
    LessonRepository,
    ModuleRepository,
    OutboxRepository,
)
from smartcourse.services.course import can_view


@dataclass(frozen=True)
class Progress:
    """How far through a course one enrollment is.

    A plain dataclass rather than a Pydantic model, because this is what a
    service returns and services know nothing about HTTP. The router turns
    it into a response; a test can read it directly.

    Frozen, so nothing downstream can quietly adjust a count on its way to
    the client and leave the database saying something else.
    """

    enrollment: Enrollment
    completed: int
    total: int
    # Whether *this* call completed the lesson, rather than it already being
    # done. The repository's insert reports it; see complete_lesson.
    newly_completed: bool = False
    # Whether *this* call finished the course. Different from
    # `enrollment.status == completed`, which stays true on every later read.
    course_just_completed: bool = False

    @property
    def percent(self) -> int:
        """Whole percent, rounded down.

        Down, so 99 lessons out of 100 never displays as 100%. Telling a
        student they are finished when they are not is worse than being a
        percentage point mean.

        A course with no lessons reports 0 rather than 100. Both are
        defensible and neither means much; 0 at least does not award an
        achievement for a course with nothing in it.
        """
        if self.total == 0:
            return 0
        return self.completed * 100 // self.total


async def _course_of_lesson(
    courses: CourseRepository,
    modules: ModuleRepository,
    lessons: LessonRepository,
    *,
    lesson_id: UUID,
    user: User,
):
    """The course a lesson belongs to, if this caller may see it at all.

    A lesson row carries no course id - the chain is lesson to module to
    course - so the walk happens here, the same way get_lesson_detail does
    it in course.py.

    Every failure is 404 with the same wording. A caller who cannot see the
    course must not be able to tell a lesson that exists from one that does
    not, and three differently worded errors would do exactly that.
    """
    lesson = await lessons.get_by_id(lesson_id)
    if lesson is None:
        raise NotFoundError("No lesson with that id.")

    module = await modules.get_by_id(lesson.module_id)
    if module is None:
        raise NotFoundError("No lesson with that id.")

    course = await courses.get_by_id(module.course_id)
    if course is None or not can_view(course, user):
        raise NotFoundError("No lesson with that id.")

    return course


async def _studying(
    enrollments: EnrollmentRepository, *, course_id: UUID, user: User
) -> Enrollment:
    """The enrollment that lets this caller record progress.

    Active only, and the two refusals are deliberately different:

        no enrollment     403 - enrol first
        already complete  409 - there is nothing left to record

    `completed` is excluded on purpose, and it is what answers "what if an
    instructor adds a lesson to a course someone has already finished?".
    They cannot complete the new lesson, and the course stays complete.

    Completion is a statement about a moment: this student finished this
    course as it stood. Re-opening it would be worse in both directions - it
    would retract an achievement nobody asked to retract, and it would pull
    the course out from under any prerequisite that now depends on it
    (FR-08 reads `completed`), so adding a lesson could silently invalidate
    a student's enrollment in a different course.
    """
    enrollment = await enrollments.get_for_content(
        student_id=user.id, course_id=course_id
    )
    if enrollment is None:
        raise PermissionDeniedError(
            "Enrol in this course before recording progress."
        )
    if enrollment.status == ENROLLMENT_COMPLETED:
        raise ConflictError("You have already completed this course.")
    return enrollment


async def complete_lesson(
    enrollments: EnrollmentRepository,
    courses: CourseRepository,
    modules: ModuleRepository,
    lessons: LessonRepository,
    outbox: OutboxRepository,
    *,
    lesson_id: UUID,
    user: User,
) -> Progress:
    """Mark one lesson finished, and finish the course if it was the last.

    FR-14 and FR-15 in one function, because they are one transaction. A
    student completing their final lesson and the course becoming complete
    must not be two states the system can be caught between.

    There is no undo. Completion is the student's own claim about their own
    learning rather than a grade someone awards, and a record that can be
    retracted is not much of a record. The cost is a misclick with no
    recovery - which is why completing a course no longer removes access to
    it, see EnrollmentRepository.get_for_content.
    """
    course = await _course_of_lesson(
        courses, modules, lessons, lesson_id=lesson_id, user=user
    )
    enrollment = await _studying(enrollments, course_id=course.id, user=user)

    newly_completed = await enrollments.mark_lesson_complete(
        enrollment_id=enrollment.id, lesson_id=lesson_id
    )

    done = await enrollments.completed_lesson_ids(enrollment.id)
    required = set(await lessons.ids_for_course(course.id))

    # FR-15. A subset test rather than `len(done) == len(required)`, because
    # it says what is meant: every lesson in the course is among the ones
    # finished. Counting would also be correct today and would stop being
    # correct the moment the two sets could differ.
    #
    # `required and` guards the empty course. An empty set is a subset of
    # everything, so without it a course with no lessons would complete -
    # though only if something called this, which nothing can. The guard is
    # for the reader as much as the logic.
    course_just_completed = bool(required) and required <= done
    if course_just_completed:
        enrollment.status = ENROLLMENT_COMPLETED
        # The column has existed since Module 1 and the schema comment said
        # "nothing sets it yet - completion arrives with FR-14/15 in Module
        # 3". This is that.
        #
        # From the application clock rather than func.now(). Writing the SQL
        # function would keep one clock for enrolled_at and completed_at,
        # which is tidier - but the value would stay a SQL expression in
        # Python until the row was refreshed, and this request returns it.
        # complete_publish set published_at the same way, for the same
        # reason.
        enrollment.completed_at = datetime.now(UTC)

        # Feeds "completion rate" and "average time to finish a course" -
        # the second being this event's occurred_at measured against the
        # student.enrolled for the same enrollment_id.
        #
        # Inside the branch, so it fires exactly once per enrollment. It
        # cannot fire twice: the moment the status becomes completed,
        # _studying raises 409 and this function never runs again for this
        # enrollment.
        outbox.add(
            events.course_completed(
                enrollment_id=enrollment.id,
                student_id=user.id,
                course_id=course.id,
            )
        )

    return Progress(
        enrollment=enrollment,
        # Intersection, matching get_progress. `len(done)` would agree today
        # and the two should not be written differently for no reason - a
        # reader will assume the difference means something. It is also the
        # safer half: counting finished lessons that are still part of the
        # course cannot report more completed than there are to complete.
        completed=len(done & required),
        total=len(required),
        newly_completed=newly_completed,
        course_just_completed=course_just_completed,
    )


async def get_progress(
    enrollments: EnrollmentRepository,
    courses: CourseRepository,
    lessons: LessonRepository,
    *,
    course_id: UUID,
    user: User,
) -> Progress:
    """How far this caller has got in one course. FR-14's second half.

    Counted on demand rather than stored. A stored percentage is wrong the
    moment an instructor adds a lesson, and nothing would recalculate it;
    "this person finished that lesson at that time" stays true whatever
    changes around it.

    Readable by a student who has *completed* the course as well as one
    still working through it - unlike recording progress, there is nothing
    odd about looking at a finished course and seeing 100%.
    """
    course = await courses.get_by_id(course_id)
    if course is None or not can_view(course, user):
        raise NotFoundError("No course with that id.")

    enrollment = await enrollments.get_for_content(
        student_id=user.id, course_id=course_id
    )
    if enrollment is None:
        raise PermissionDeniedError("You are not enrolled in this course.")

    done = await enrollments.completed_lesson_ids(enrollment.id)
    required = set(await lessons.ids_for_course(course_id))

    return Progress(
        enrollment=enrollment,
        # Intersection rather than len(done), the same way complete_lesson
        # counts it. They differ only if a lesson could be finished and
        # then leave the course - which cascade deletes today, so the two
        # agree. Written this way so the count cannot exceed the total if
        # that ever stops being true.
        completed=len(done & required),
        total=len(required),
    )
