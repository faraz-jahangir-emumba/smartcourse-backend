"""Progress shapes. FR-14, FR-15.

No request body here either, for the same reason as enrollment.py: marking
a lesson finished needs two facts, which lesson and who, and both already
travel with the request - the lesson in the path, the student in the token.

Nothing in this file is writable. `percent` and the counts are derived, and
a client that could send them could disagree with the database.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from smartcourse.services.progress import Progress


class ProgressResponse(BaseModel):
    """How far one student is through one course.

    Counts *and* the percentage, rather than making the client divide. The
    division has an edge case - a course with no lessons - and answering it
    once here is better than every client answering it differently, or
    dividing by zero.
    """

    course_id: UUID
    enrollment_id: UUID
    # The enrollment's status: active, or completed once every lesson is
    # done. Withdrawn never appears - a withdrawn student cannot read this.
    status: str
    completed: int
    total: int
    percent: int
    completed_at: datetime | None

    @classmethod
    def build(cls, progress: Progress) -> "ProgressResponse":
        enrollment = progress.enrollment
        return cls(
            course_id=enrollment.course_id,
            enrollment_id=enrollment.id,
            status=enrollment.status,
            completed=progress.completed,
            total=progress.total,
            percent=progress.percent,
            completed_at=enrollment.completed_at,
        )


class LessonCompletionResponse(ProgressResponse):
    """What POST /lessons/{id}/completion returns.

    The whole progress, not just an acknowledgement, so a client can redraw
    its bar without a second call. The two extra flags are about *this*
    request rather than the current state, which is a distinction a client
    actually needs:

        newly_completed   false if the lesson was already done. Lets the UI
                          stay quiet on a double-click instead of
                          congratulating somebody twice

        course_completed  true only on the request that finished the course.
                          `status` says whether it is complete; this says
                          whether it just became so, which is when to show
                          the banner
    """

    lesson_id: UUID
    newly_completed: bool
    course_completed: bool

    @classmethod
    def build_for_lesson(
        cls, progress: Progress, *, lesson_id: UUID
    ) -> "LessonCompletionResponse":
        base = ProgressResponse.build(progress)
        return cls(
            **base.model_dump(),
            lesson_id=lesson_id,
            newly_completed=progress.newly_completed,
            course_completed=progress.course_just_completed,
        )
