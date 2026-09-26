"""Course, module and lesson use cases. FR-04, FR-05.

No queries here - those live in the repositories. What is left is the rules
and the order they apply in.

Ownership is enforced at this level rather than in the API layer. "Only the
instructor who owns a course may change it" is a business rule, and it has to
hold for every caller, including the Temporal workflows and Celery tasks of
later modules, which never touch an HTTP route.

The division: the API checks *roles* - are you an instructor at all. This
checks *ownership* - is this your course. Being an instructor gets you
through the door; owning the course is what lets you edit that one.
"""

from uuid import UUID

from smartcourse.domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from smartcourse.infra.db.models.course import (
    COURSE_STATUSES,
    Course,
    Lesson,
    Module,
)
from smartcourse.infra.db.models.user import User
from smartcourse.infra.db.repositories import (
    CourseRepository,
    LessonRepository,
    ModuleRepository,
)

# Only a draft can be freely edited. Once a course is published students may
# be part-way through it, so changing its shape underneath them is a different
# problem - one Module 2 deals with properly. `failed` is editable because a
# failed publish leaves a course that needs fixing.
EDITABLE_STATUSES = ("draft", "failed")


def _assert_can_edit(course: Course, user: User) -> None:
    """The owner, or an admin. Nobody else."""
    if "admin" in user.roles:
        return
    if course.instructor_id != user.id:
        # 403 rather than 404. A published course is public, so hiding its
        # existence achieves nothing; saying plainly that it is not yours is
        # more useful than pretending it is missing.
        raise PermissionDeniedError("This course belongs to another instructor.")


def _can_view(course: Course, user: User) -> bool:
    """Whether this course is visible to this caller.

    The single-course counterpart of the visibility filter in list_courses.
    Both express the same rule; keeping them consistent matters, because a
    filtered list with an unfiltered detail endpoint is not a rule at all.
    """
    if course.status == "ready":
        return True
    return "admin" in user.roles or course.instructor_id == user.id


def _assert_editable(course: Course) -> None:
    if course.status not in EDITABLE_STATUSES:
        raise ConflictError(
            f"A course in status '{course.status}' cannot be edited.",
            details={"editable_in": list(EDITABLE_STATUSES)},
        )


async def _course_or_404(courses: CourseRepository, course_id: UUID) -> Course:
    course = await courses.get_by_id(course_id)
    if course is None:
        raise NotFoundError("No course with that id.")
    return course


async def _module_or_404(modules: ModuleRepository, module_id: UUID) -> Module:
    module = await modules.get_by_id(module_id)
    if module is None:
        raise NotFoundError("No module with that id.")
    return module


async def _lesson_or_404(lessons: LessonRepository, lesson_id: UUID) -> Lesson:
    lesson = await lessons.get_by_id(lesson_id)
    if lesson is None:
        raise NotFoundError("No lesson with that id.")
    return lesson


def _reposition(items: list, order: list[UUID], what: str) -> None:
    """Rewrite positions from a complete list of ids.

    The whole order, never a single move - "put this one at position 2" is
    ambiguous between swapping two items and shifting a run of them, and the
    two give different results.

    A partial list is rejected rather than applied. Accepting one would leave
    the items not mentioned at stale positions, colliding with the new ones.

    Positions are briefly duplicated while this runs, which is exactly why
    the unique constraint is DEFERRABLE INITIALLY DEFERRED: Postgres checks
    it at COMMIT rather than after each statement.
    """
    by_id = {item.id: item for item in items}
    if set(order) != set(by_id):
        raise ValidationError(
            f"{what} must list every one exactly once.",
            details={"expected": [str(i) for i in by_id]},
        )
    for position, item_id in enumerate(order):
        by_id[item_id].position = position


# --------------------------------------------------------------- courses ---


async def create_course(
    courses: CourseRepository,
    *,
    instructor: User,
    title: str,
    description: str | None,
    capacity: int | None,
) -> Course:
    """Create a draft course owned by this instructor. FR-04.

    Status is not a parameter. A course always starts as a draft - you
    publish it later, and the status follows from how that goes.
    """
    return await courses.add(
        Course(
            instructor_id=instructor.id,
            title=title.strip(),
            description=description,
            capacity=capacity,
            status="draft",
        )
    )


async def list_courses(
    courses: CourseRepository,
    *,
    user: User,
    status: str | None = None,
    instructor_id: UUID | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Course], int]:
    """A page of courses the caller is allowed to see. FR-05.

    FR-05 promises students browse *published* courses. A draft is by
    definition unfinished - Part A section 2 says a course is marked ready
    only once all internal processing completes - so showing one to a student
    exposes work that is not meant to be seen, including other instructors'.

    Who sees what:

        student     ready courses only
        instructor  ready courses, plus their own in any state
        admin       everything

    The caller's own `status` filter narrows this; it can never widen it. A
    status they may not ask about is refused rather than quietly returning
    nothing - see below.
    """
    is_admin = "admin" in user.roles
    is_instructor = "instructor" in user.roles

    if is_admin:
        visible_statuses = None  # no restriction
        also_owned_by = None
    else:
        visible_statuses = ("ready",)
        # An instructor also sees their own drafts. A student owns nothing,
        # so this adds nothing for them.
        also_owned_by = user.id if is_instructor else None

    # Instructors and admins may filter by any status - the visibility rule
    # above already limits an instructor to their own unpublished work. A
    # student has exactly one meaningful value.
    filterable = COURSE_STATUSES if (is_admin or is_instructor) else ("ready",)

    if status is not None:
        if status not in COURSE_STATUSES:
            # Not a real status at all. A malformed request, not a
            # permission problem.
            raise ValidationError(
                f"Unknown status '{status}'.",
                details={"valid": list(COURSE_STATUSES)},
            )
        if status not in filterable:
            # A real status this caller may not ask about.
            #
            # Refused rather than returning an empty page, because the two
            # are indistinguishable to a client - it would render "no courses
            # found", which is untrue. "You may not ask that" is the honest
            # answer, and that a draft state exists is already public in the
            # API documentation.
            raise PermissionDeniedError(
                "Students can only browse published courses.",
                details={"allowed": list(filterable)},
            )

    return await courses.list(
        status=status,
        instructor_id=instructor_id,
        visible_statuses=visible_statuses,
        also_owned_by=also_owned_by,
        limit=limit,
        offset=offset,
    )


async def get_course_detail(
    courses: CourseRepository, *, course_id: UUID, user: User
) -> Course:
    """One course with its modules and lessons.

    The same visibility rule as the listing, and it has to be here too -
    filtering a list is pointless if the id can be fetched directly.

    A course the caller may not see returns 404, not 403. 403 would confirm
    it exists, which is exactly what is being withheld: knowing an instructor
    has an unannounced course in draft is itself information.
    """
    course = await courses.get_with_contents(course_id)
    if course is None:
        raise NotFoundError("No course with that id.")

    if not _can_view(course, user):
        raise NotFoundError("No course with that id.")

    return course


async def get_module_detail(
    courses: CourseRepository,
    modules: ModuleRepository,
    *,
    module_id: UUID,
    user: User,
) -> Module:
    """One module with its lessons.

    Visibility is decided by the parent course, never by the module itself.
    Without walking up, this endpoint would be a way round the course rule:
    a student who knew a module id could read a draft course's contents one
    module at a time.
    """
    module = await modules.get_with_lessons(module_id)
    if module is None:
        raise NotFoundError("No module with that id.")

    course = await _course_or_404(courses, module.course_id)
    if not _can_view(course, user):
        raise NotFoundError("No module with that id.")

    return module


async def get_lesson_detail(
    courses: CourseRepository,
    modules: ModuleRepository,
    lessons: LessonRepository,
    *,
    lesson_id: UUID,
    user: User,
) -> tuple[Lesson, UUID]:
    """One lesson, and the id of the course it belongs to.

    The course id is returned alongside because a lesson row does not carry
    one - the chain is lesson to module to course. It has to be loaded here
    for the visibility check anyway, so handing it back costs nothing and
    saves the client a second call to find out where it is.
    """
    lesson = await _lesson_or_404(lessons, lesson_id)
    module = await _module_or_404(modules, lesson.module_id)
    course = await _course_or_404(courses, module.course_id)

    if not _can_view(course, user):
        raise NotFoundError("No lesson with that id.")

    return lesson, course.id


async def update_course(
    courses: CourseRepository,
    modules: ModuleRepository,
    *,
    course_id: UUID,
    user: User,
    title: str | None = None,
    description: str | None = None,
    capacity: int | None = None,
    module_order: list[UUID] | None = None,
) -> Course:
    """Change a course. Only the fields that were sent.

    `None` means "not mentioned", so an omitted field keeps its value. That
    is what PATCH means, and it is why a field cannot be cleared by accident.
    """
    course = await _course_or_404(courses, course_id)
    _assert_can_edit(course, user)
    _assert_editable(course)

    if title is not None:
        course.title = title.strip()
    if description is not None:
        course.description = description
    if capacity is not None:
        course.capacity = capacity

    if module_order is not None:
        existing = await modules.list_for_course(course.id)
        _reposition(existing, module_order, "module_order")
        await modules.flush()

    return course


async def delete_course(
    courses: CourseRepository, *, course_id: UUID, user: User
) -> None:
    course = await _course_or_404(courses, course_id)
    _assert_can_edit(course, user)
    await courses.delete(course)


# --------------------------------------------------------------- modules ---


async def add_module(
    courses: CourseRepository,
    modules: ModuleRepository,
    *,
    course_id: UUID,
    user: User,
    title: str,
) -> Module:
    course = await _course_or_404(courses, course_id)
    _assert_can_edit(course, user)
    _assert_editable(course)

    return await modules.add(
        Module(
            course_id=course.id,
            title=title.strip(),
            position=await modules.next_position(course.id),
        )
    )


async def update_module(
    courses: CourseRepository,
    modules: ModuleRepository,
    lessons: LessonRepository,
    *,
    module_id: UUID,
    user: User,
    title: str | None = None,
    lesson_order: list[UUID] | None = None,
) -> Module:
    module = await _module_or_404(modules, module_id)
    course = await _course_or_404(courses, module.course_id)
    _assert_can_edit(course, user)
    _assert_editable(course)

    if title is not None:
        module.title = title.strip()

    if lesson_order is not None:
        existing = await lessons.list_for_module(module.id)
        _reposition(existing, lesson_order, "lesson_order")
        await lessons.flush()

    await modules.flush()
    return module


async def delete_module(
    courses: CourseRepository,
    modules: ModuleRepository,
    *,
    module_id: UUID,
    user: User,
) -> None:
    module = await _module_or_404(modules, module_id)
    course = await _course_or_404(courses, module.course_id)
    _assert_can_edit(course, user)
    _assert_editable(course)
    await modules.delete(module)


# --------------------------------------------------------------- lessons ---


async def add_lesson(
    courses: CourseRepository,
    modules: ModuleRepository,
    lessons: LessonRepository,
    *,
    module_id: UUID,
    user: User,
    title: str,
    content: str | None,
) -> Lesson:
    module = await _module_or_404(modules, module_id)
    course = await _course_or_404(courses, module.course_id)
    _assert_can_edit(course, user)
    _assert_editable(course)

    return await lessons.add(
        Lesson(
            module_id=module.id,
            title=title.strip(),
            content=content,
            position=await lessons.next_position(module.id),
        )
    )


async def update_lesson(
    courses: CourseRepository,
    modules: ModuleRepository,
    lessons: LessonRepository,
    *,
    lesson_id: UUID,
    user: User,
    title: str | None = None,
    content: str | None = None,
) -> Lesson:
    lesson = await _lesson_or_404(lessons, lesson_id)
    module = await _module_or_404(modules, lesson.module_id)
    course = await _course_or_404(courses, module.course_id)
    _assert_can_edit(course, user)
    _assert_editable(course)

    if title is not None:
        lesson.title = title.strip()
    if content is not None:
        lesson.content = content

    await lessons.flush()
    return lesson


async def delete_lesson(
    courses: CourseRepository,
    modules: ModuleRepository,
    lessons: LessonRepository,
    *,
    lesson_id: UUID,
    user: User,
) -> None:
    lesson = await _lesson_or_404(lessons, lesson_id)
    module = await _module_or_404(modules, lesson.module_id)
    course = await _course_or_404(courses, module.course_id)
    _assert_can_edit(course, user)
    _assert_editable(course)
    await lessons.delete(lesson)
