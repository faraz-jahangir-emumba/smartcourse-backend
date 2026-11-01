"""Marking lessons finished, and reading how far you are. FR-14, FR-15.

Two endpoints, two nouns:

    POST /lessons/{id}/completion     record that I finished this lesson
    GET  /courses/{id}/progress       how far am I through this course

`completion` rather than `complete`, because a noun says what is being
created and a verb only says what is being done. It also cannot be read as
a property of the lesson the way `status` could - a lesson is not
completed, a person completes it, and two students see different answers
for the same lesson.

There is no way to undo one. Completion is the student's own claim about
their own learning rather than a grade somebody awards, and a record that
can be retracted is not much of a record.
"""

from uuid import UUID

from fastapi import APIRouter

from smartcourse.api.deps import (
    CourseRepo,
    CurrentUser,
    EnrollmentRepo,
    LessonRepo,
    ModuleRepo,
)
from smartcourse.api.schemas.progress import (
    LessonCompletionResponse,
    ProgressResponse,
)
from smartcourse.services import progress as progress_service

router = APIRouter(tags=["progress"])


@router.post(
    "/lessons/{lesson_id}/completion",
    response_model=LessonCompletionResponse,
    summary="Mark a lesson finished",
)
async def complete_lesson(
    lesson_id: UUID,
    enrollments: EnrollmentRepo,
    courses: CourseRepo,
    modules: ModuleRepo,
    lessons: LessonRepo,
    user: CurrentUser,
) -> LessonCompletionResponse:
    """200, and the progress that results.

    Not 201, although this creates a row. 201 promises something was
    created, and the second identical request creates nothing - the unique
    index sees to that. Returning 201 to a call that inserted nothing would
    be a lie, and alternating 201/200 would make clients branch on a
    distinction `newly_completed` already carries in the body.

    No `dependencies=[StudentOnly]`. The rule that matters is not "are you a
    student" but "do you have an active enrollment in this course", which
    only the service can answer - and in Area B a Celery task or a Kafka
    consumer may need the same use case without ever passing through a
    route.
    """
    result = await progress_service.complete_lesson(
        enrollments,
        courses,
        modules,
        lessons,
        lesson_id=lesson_id,
        user=user,
    )
    return LessonCompletionResponse.build_for_lesson(result, lesson_id=lesson_id)


@router.get(
    "/courses/{course_id}/progress",
    response_model=ProgressResponse,
    summary="My progress through a course",
)
async def get_progress(
    course_id: UUID,
    enrollments: EnrollmentRepo,
    courses: CourseRepo,
    lessons: LessonRepo,
    user: CurrentUser,
) -> ProgressResponse:
    """Course-scoped rather than enrollment-scoped.

    `/users/me/enrollments/{id}/progress` would be more literal - progress
    does belong to an enrollment - but it makes a client fetch the
    enrollment id first to ask a question about a course it is already
    looking at. The server can find the enrollment from the course and the
    token, so making the caller supply it is asking for something it should
    not need to know.
    """
    result = await progress_service.get_progress(
        enrollments, courses, lessons, course_id=course_id, user=user
    )
    return ProgressResponse.build(result)
