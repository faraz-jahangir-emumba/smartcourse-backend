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
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from smartcourse.api.deps import (
    CourseRepo,
    CurrentUser,
    EnrollmentRepo,
    LessonRepo,
    ModuleRepo,
    WorkflowClient,
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
    LessonListResponse,
    LessonResponse,
    LessonUpdate,
    ModuleCreate,
    ModuleDetailResponse,
    ModuleResponse,
    ModuleUpdate,
    PublishAccepted,
    PublishStatus,
)
from smartcourse.domain.errors import ConflictError
from smartcourse.infra.db.models.user import ROLE_ADMIN, ROLE_INSTRUCTOR
from smartcourse.services import course as course_service
from smartcourse.services import publishing as publishing_service
from smartcourse.workflows.publishing import (
    TASK_QUEUE,
    PublishCourseWorkflow,
    workflow_id_for,
)

router = APIRouter(tags=["courses"])

# Named once, applied to every route that writes.
InstructorOnly = Depends(require_roles(ROLE_INSTRUCTOR, ROLE_ADMIN))


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
        prerequisite_ids=payload.prerequisite_ids,
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
    q: str | None = Query(
        default=None,
        min_length=1,
        max_length=200,
        description="Free text. Searches titles and descriptions.",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> CourseListResponse:
    items, total = await course_service.list_courses(
        courses,
        user=user,
        status=status_filter,
        instructor_id=instructor_id,
        search=q,
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
    course_id: UUID,
    courses: CourseRepo,
    enrollments: EnrollmentRepo,
    user: CurrentUser,
) -> CourseDetailResponse:
    """The whole course. Lesson text only for those who may read it.

    A caller who is not enrolled still gets the structure - every module
    and lesson, with titles and order - because that is the syllabus, and
    it is how somebody decides whether to enrol. Each locked lesson comes
    back with `content: null` and `content_locked: true`.
    """
    course, may_read = await course_service.get_course_detail(
        courses, enrollments, course_id=course_id, user=user
    )

    detail = CourseDetailResponse.model_validate(course)
    if not may_read:
        for module in detail.modules:
            module.lessons = [
                LessonResponse.without_content(lesson) for lesson in module.lessons
            ]
    return detail


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
        prerequisite_ids=payload.prerequisite_ids,
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
    "/courses/{course_id}/publish",
    response_model=PublishAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    # The route asks "are you an instructor at all"; the service asks "is
    # this your course". Publishing skipped the first half, so a student
    # reached the ownership check and was told the course did not exist -
    # about a course they could read in the catalogue.
    dependencies=[InstructorOnly],
    summary="Publish a course",
)
async def publish_course(
    course_id: UUID,
    courses: CourseRepo,
    user: CurrentUser,
    client: WorkflowClient,
) -> PublishAccepted:
    """202 Accepted, immediately. The work happens elsewhere.

    Not 200, and not 201. 202 means "I have taken this and will do it",
    which is exactly true: chunking a 200-lesson course is not something an
    HTTP request should sit through, and the caller gets an answer in
    milliseconds either way.

    The permission check runs here rather than in the workflow. Refusing
    belongs in the request that made it, where there is a caller to refuse;
    a background job has already been accepted and has nobody to tell.

    Every status write happens in the workflow, never here. Postgres and
    Temporal share no transaction, so an endpoint doing both could do one
    and not the other - commit first and a failed start strands the course
    on `publishing`, start first and a failed commit leaves the workflow
    running against a draft.
    """
    await publishing_service.assert_can_publish(
        courses, course_id=course_id, user=user
    )

    workflow_id = workflow_id_for(str(course_id))

    try:
        await client.start_workflow(
            PublishCourseWorkflow.run,
            str(course_id),
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError as exc:
        # Temporal refuses a second workflow with the same id while one is
        # running. That is the real protection against a double-click, not
        # the status check above - which two simultaneous requests would
        # both pass.
        raise ConflictError(
            "This course is already being published.",
            details={"workflow_id": workflow_id},
        ) from exc

    return PublishAccepted(
        course_id=course_id,
        workflow_id=workflow_id,
        status_url=f"/api/v1/courses/{course_id}/publish/status",
    )


@router.get(
    "/courses/{course_id}/publish/status",
    response_model=PublishStatus,
    dependencies=[InstructorOnly],
    summary="How the last publish of this course went",
)
async def get_publish_status(
    course_id: UUID,
    courses: CourseRepo,
    user: CurrentUser,
    client: WorkflowClient,
) -> PublishStatus:
    """What the 202 promised to be able to tell you later.

    This endpoint exists because a republish that fails is invisible. It
    leaves the course `ready` - correctly, since the live material is
    untouched and the old chunks still serve - which means the course's own
    status can no longer answer "did my publish work".

    Temporal can. The outcome is already recorded durably; it simply was not
    reachable from the API, so it was visible to whoever runs the Temporal
    UI and to nobody else.

    No new table and no new writes: this reads a fact that already exists.
    Turning a failed publish into something an instructor is *told* about,
    rather than something they can look up, needs the FailedEvent table and
    belongs with Module 3's event work.

    Not public. The permission ladder is the publishing one - a student has
    no business asking how someone else's publish went, and the 404/403
    split matches POST /publish so the two cannot disagree.
    """
    course = await publishing_service.assert_publishing_access(
        courses, course_id=course_id, user=user
    )
    workflow_id = workflow_id_for(str(course_id))

    state = "unknown"
    started_at = None
    finished_at = None
    chunks_written = None

    try:
        handle = client.get_workflow_handle(workflow_id)
        description = await handle.describe()
    except RPCError as exc:
        # NOT_FOUND is an ordinary answer, not a failure: the course was
        # never published, or the history has aged out of Temporal's
        # retention. Both leave `state` as "unknown", and the durable
        # columns below still say whether the course is live.
        if exc.status is not RPCStatusCode.NOT_FOUND:
            raise
    else:
        state = description.status.name.lower() if description.status else "unknown"
        started_at = description.start_time
        finished_at = description.close_time

        if description.status is WorkflowExecutionStatus.COMPLETED:
            # Safe to await only because it has finished - on a running
            # workflow this call blocks until it does, which is not what an
            # HTTP handler should do. The number is the workflow's return
            # value, so there is nowhere else to read it from.
            chunks_written = await handle.result()

    return PublishStatus(
        course_id=course_id,
        course_status=course.status,
        published_at=course.published_at,
        workflow_id=workflow_id,
        state=state,
        started_at=started_at,
        finished_at=finished_at,
        chunks_written=chunks_written,
    )


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
    "/courses/{course_id}/lessons",
    response_model=LessonListResponse,
    summary="A course's lessons, or search within them",
)
async def list_lessons(
    course_id: UUID,
    courses: CourseRepo,
    lessons: LessonRepo,
    enrollments: EnrollmentRepo,
    user: CurrentUser,
    q: str | None = Query(
        default=None,
        min_length=1,
        max_length=200,
        description="Free text. Searches lesson titles and content.",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> LessonListResponse:
    """FR-10c. Reading order when browsing, best match first when searching.

    A noun with an optional filter rather than a /search verb. Listing a
    course's lessons is useful on its own - there was no flat list before
    this, only the whole nested course - and searching is that same list,
    narrowed.

    Scoped to one course because that is the question a student has: they
    are reading something and want the lesson that covers a topic. A global
    search would have to apply every course's visibility rules row by row.
    """
    items, total, may_read = await course_service.list_lessons(
        courses,
        lessons,
        enrollments,
        course_id=course_id,
        user=user,
        search=q,
        limit=limit,
        offset=offset,
    )
    return LessonListResponse(
        items=[
            LessonResponse.model_validate(lesson)
            if may_read
            else LessonResponse.without_content(lesson)
            for lesson in items
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/modules/{module_id}",
    response_model=ModuleDetailResponse,
    summary="One module, with its lessons",
)
async def get_module(
    module_id: UUID,
    courses: CourseRepo,
    modules: ModuleRepo,
    enrollments: EnrollmentRepo,
    user: CurrentUser,
) -> ModuleDetailResponse:
    """One module and its lessons, with the same gate as everywhere else.

    This endpoint returns lesson content, so it needs the enrolment check
    exactly as GET /courses/{id} does - without it a non-enrolled student
    reads a whole course one module at a time.
    """
    module, may_read = await course_service.get_module_detail(
        courses, modules, enrollments, module_id=module_id, user=user
    )

    detail = ModuleDetailResponse.model_validate(module)
    if not may_read:
        detail.lessons = [
            LessonResponse.without_content(lesson) for lesson in detail.lessons
        ]
    return detail


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
    enrollments: EnrollmentRepo,
    user: CurrentUser,
) -> LessonDetailResponse:
    """Built field by field rather than with model_validate, because
    course_id is not a column on a lesson - the service supplies it from the
    visibility check it had to do anyway."""
    lesson, course_id = await course_service.get_lesson_detail(
        courses, modules, lessons, enrollments, lesson_id=lesson_id, user=user
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
