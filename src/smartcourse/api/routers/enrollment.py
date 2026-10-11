"""Enrolling, withdrawing, and reading who is on a course. FR-06 to FR-10.

Two verbs and two nouns, which is a deliberate split. Enrolling and
withdrawing are named state transitions with rules that can refuse them, so
they read as actions. A roster is a collection, so it reads as one.

Everything in docs/API.md before this was a noun. The line being held is
that a verb earns its place only when a transition has a name and rules -
not as a general escape hatch. `publish` is the next one, later in Module 2.
"""

from uuid import UUID

from fastapi import APIRouter, Query, status

from smartcourse.api.deps import CourseRepo, CurrentUser, EnrollmentRepo
from smartcourse.api.schemas.enrollment import (
    EnrollmentListResponse,
    EnrollmentResponse,
)
from smartcourse.services import enrollment as enrollment_service

router = APIRouter(tags=["enrollment"])


@router.post(
    "/courses/{course_id}/enrollments/enroll",
    response_model=EnrollmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Enrol in a course",
)
async def enroll(
    course_id: UUID,
    enrollments: EnrollmentRepo,
    courses: CourseRepo,
    user: CurrentUser,
) -> EnrollmentResponse:
    """201 and the new enrollment.

    No `dependencies=[StudentOnly]` here, deliberately. The role check lives
    in the service alongside the other three rules, because a Module 3 Celery
    task enrolling somebody must obey it too and will never pass through a
    route.
    """
    record = await enrollment_service.enroll(
        enrollments, courses, course_id=course_id, user=user
    )
    return EnrollmentResponse.model_validate(record)


@router.post(
    "/courses/{course_id}/enrollments/withdraw",
    response_model=EnrollmentResponse,
    summary="Leave a course",
)
async def withdraw(
    course_id: UUID,
    enrollments: EnrollmentRepo,
    user: CurrentUser,
) -> EnrollmentResponse:
    """200 and the withdrawn enrollment, not 204.

    Returning the row makes it visible that nothing was deleted - the status
    changed and the record survives, which is what FR-10 requires. A 204
    would look like a deletion and quietly teach clients the wrong model.
    """
    record = await enrollment_service.withdraw(
        enrollments, course_id=course_id, user=user
    )
    return EnrollmentResponse.model_validate(record)


@router.get(
    "/courses/{course_id}/enrollments",
    response_model=EnrollmentListResponse,
    summary="Who is on this course",
)
async def list_course_enrollments(
    course_id: UUID,
    enrollments: EnrollmentRepo,
    courses: CourseRepo,
    user: CurrentUser,
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="active, completed or withdrawn. Omit for all three.",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> EnrollmentListResponse:
    """The owning instructor, or an admin. Not the students on it.

    Who else is taking a course is other people's business, and a class list
    is personal data. A caller with no claim to the course gets 404 rather
    than 403, matching the read rules for courses themselves.
    """
    items, total = await enrollment_service.list_course_enrollments(
        enrollments,
        courses,
        course_id=course_id,
        user=user,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return EnrollmentListResponse(
        items=[EnrollmentResponse.model_validate(e) for e in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/users/me/enrollments",
    response_model=EnrollmentListResponse,
    summary="Courses I have joined",
)
async def list_my_enrollments(
    enrollments: EnrollmentRepo,
    user: CurrentUser,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> EnrollmentListResponse:
    """Every status, including withdrawn.

    FR-10 keeps history so it can be read. Filtering it out here would make
    the requirement pointless - and "courses I started and left" is a
    reasonable thing to want to see.
    """
    items, total = await enrollment_service.list_my_enrollments(
        enrollments, user=user, limit=limit, offset=offset
    )
    return EnrollmentListResponse(
        items=[EnrollmentResponse.model_validate(e) for e in items],
        total=total,
        limit=limit,
        offset=offset,
    )
