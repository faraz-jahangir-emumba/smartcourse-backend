"""Course, module and lesson endpoints. FR-04, FR-05.

Two levels of check, doing different jobs:

- `Depends(require_roles(...))` on the route - are you an instructor at all?
  Enforced here, at the door.
- ownership inside the service - is this *your* course? Enforced there,
  because it is a business rule that must hold for every caller, including
  the background workers of later modules.

Reading is open to anyone signed in; writing is not.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from smartcourse.api.deps import (
    CourseRepo,
    CurrentUser,
    LessonRepo,
    ModuleRepo,
    require_roles,
)
from smartcourse.api.schemas.course import (
    CourseCreate,
    CourseDetailResponse,
    CourseListResponse,
    CourseResponse,
    CourseUpdate,
    LessonCreate,
    LessonDetailResponse,
    LessonResponse,
    LessonUpdate,
    ModuleCreate,
    ModuleDetailResponse,
    ModuleResponse,
    ModuleUpdate,
)
from smartcourse.services import course as course_service

router = APIRouter(tags=["courses"])

# Named once, applied to every route that writes.
InstructorOnly = Depends(require_roles("instructor", "admin"))


# --------------------------------------------------------------- courses ---


@router.post(
    "/courses",
    response_model=CourseResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[InstructorOnly],
    summary="Create a course",
)
async def create_course(
    payload: CourseCreate, courses: CourseRepo, user: CurrentUser
) -> CourseResponse:
    course = await course_service.create_course(
        courses,
        instructor=user,
        title=payload.title,
        description=payload.description,
        capacity=payload.capacity,
    )
    return CourseResponse.model_validate(course)


@router.get("/courses", response_model=CourseListResponse, summary="List courses")
async def list_courses(
    courses: CourseRepo,
    user: CurrentUser,
    status_filter: str | None = Query(default=None, alias="status"),
    instructor_id: UUID | None = None,
    # Bounded on purpose. Without a ceiling, `?limit=1000000` is a way to make
    # the server build an enormous response on demand.
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> CourseListResponse:
    items, total = await course_service.list_courses(
        courses,
        user=user,
        status=status_filter,
        instructor_id=instructor_id,
        limit=limit,
        offset=offset,
    )
    return CourseListResponse(
        items=[CourseResponse.model_validate(c) for c in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/courses/{course_id}",
    response_model=CourseDetailResponse,
    summary="One course, with its modules and lessons",
)
async def get_course(
    course_id: UUID, courses: CourseRepo, user: CurrentUser
) -> CourseDetailResponse:
    course = await course_service.get_course_detail(
        courses, course_id=course_id, user=user
    )
    return CourseDetailResponse.model_validate(course)


@router.patch(
    "/courses/{course_id}",
    response_model=CourseResponse,
    dependencies=[InstructorOnly],
    summary="Edit a course, or reorder its modules",
)
async def update_course(
    course_id: UUID,
    payload: CourseUpdate,
    courses: CourseRepo,
    modules: ModuleRepo,
    user: CurrentUser,
) -> CourseResponse:
    course = await course_service.update_course(
        courses,
        modules,
        course_id=course_id,
        user=user,
        title=payload.title,
        description=payload.description,
        capacity=payload.capacity,
        module_order=payload.module_order,
    )
    return CourseResponse.model_validate(course)


@router.delete(
    "/courses/{course_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[InstructorOnly],
    summary="Delete a course",
)
async def delete_course(
    course_id: UUID, courses: CourseRepo, user: CurrentUser
) -> None:
    """204 No Content - it worked, and there is nothing to return."""
    await course_service.delete_course(courses, course_id=course_id, user=user)


# --------------------------------------------------------------- modules ---


@router.post(
    "/courses/{course_id}/modules",
    response_model=ModuleResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[InstructorOnly],
    summary="Add a module",
)
async def add_module(
    course_id: UUID,
    payload: ModuleCreate,
    courses: CourseRepo,
    modules: ModuleRepo,
    user: CurrentUser,
) -> ModuleResponse:
    """Appended to the end. Position is not something a caller sets - it is
    the outcome of adding or reordering."""
    module = await course_service.add_module(
        courses, modules, course_id=course_id, user=user, title=payload.title
    )
    return ModuleResponse.model_validate(module)


@router.get(
    "/modules/{module_id}",
    response_model=ModuleDetailResponse,
    summary="One module, with its lessons",
)
async def get_module(
    module_id: UUID, courses: CourseRepo, modules: ModuleRepo, user: CurrentUser
) -> ModuleDetailResponse:
    module = await course_service.get_module_detail(
        courses, modules, module_id=module_id, user=user
    )
    return ModuleDetailResponse.model_validate(module)


@router.patch(
    "/modules/{module_id}",
    response_model=ModuleResponse,
    dependencies=[InstructorOnly],
    summary="Edit a module, or reorder its lessons",
)
async def update_module(
    module_id: UUID,
    payload: ModuleUpdate,
    courses: CourseRepo,
    modules: ModuleRepo,
    lessons: LessonRepo,
    user: CurrentUser,
) -> ModuleResponse:
    module = await course_service.update_module(
        courses,
        modules,
        lessons,
        module_id=module_id,
        user=user,
        title=payload.title,
        lesson_order=payload.lesson_order,
    )
    return ModuleResponse.model_validate(module)


@router.delete(
    "/modules/{module_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[InstructorOnly],
    summary="Delete a module and its lessons",
)
async def delete_module(
    module_id: UUID, courses: CourseRepo, modules: ModuleRepo, user: CurrentUser
) -> None:
    await course_service.delete_module(
        courses, modules, module_id=module_id, user=user
    )


# --------------------------------------------------------------- lessons ---


@router.post(
    "/modules/{module_id}/lessons",
    response_model=LessonResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[InstructorOnly],
    summary="Add a lesson",
)
async def add_lesson(
    module_id: UUID,
    payload: LessonCreate,
    courses: CourseRepo,
    modules: ModuleRepo,
    lessons: LessonRepo,
    user: CurrentUser,
) -> LessonResponse:
    lesson = await course_service.add_lesson(
        courses,
        modules,
        lessons,
        module_id=module_id,
        user=user,
        title=payload.title,
        content=payload.content,
    )
    return LessonResponse.model_validate(lesson)


@router.get(
    "/lessons/{lesson_id}",
    response_model=LessonDetailResponse,
    summary="One lesson",
)
async def get_lesson(
    lesson_id: UUID,
    courses: CourseRepo,
    modules: ModuleRepo,
    lessons: LessonRepo,
    user: CurrentUser,
) -> LessonDetailResponse:
    """Built field by field rather than with model_validate, because
    course_id is not a column on a lesson - the service supplies it from the
    visibility check it had to do anyway."""
    lesson, course_id = await course_service.get_lesson_detail(
        courses, modules, lessons, lesson_id=lesson_id, user=user
    )
    return LessonDetailResponse(
        id=lesson.id,
        module_id=lesson.module_id,
        course_id=course_id,
        title=lesson.title,
        content=lesson.content,
        position=lesson.position,
    )


@router.patch(
    "/lessons/{lesson_id}",
    response_model=LessonResponse,
    dependencies=[InstructorOnly],
    summary="Edit a lesson",
)
async def update_lesson(
    lesson_id: UUID,
    payload: LessonUpdate,
    courses: CourseRepo,
    modules: ModuleRepo,
    lessons: LessonRepo,
    user: CurrentUser,
) -> LessonResponse:
    lesson = await course_service.update_lesson(
        courses,
        modules,
        lessons,
        lesson_id=lesson_id,
        user=user,
        title=payload.title,
        content=payload.content,
    )
    return LessonResponse.model_validate(lesson)


@router.delete(
    "/lessons/{lesson_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[InstructorOnly],
    summary="Delete a lesson",
)
async def delete_lesson(
    lesson_id: UUID,
    courses: CourseRepo,
    modules: ModuleRepo,
    lessons: LessonRepo,
    user: CurrentUser,
) -> None:
    await course_service.delete_lesson(
        courses, modules, lessons, lesson_id=lesson_id, user=user
    )
