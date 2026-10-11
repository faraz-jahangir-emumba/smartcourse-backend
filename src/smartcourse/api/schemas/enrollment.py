"""Enrollment shapes.

No request body anywhere. Enrolling needs two facts - which course, and who -
and both already travel with the request: the course in the path, the student
in the token. A body would be a third place to say the same thing, and a
place to disagree with it.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class EnrollmentResponse(BaseModel):
    """One enrollment.

    `status` is readable but never writable - there is no EnrollmentUpdate in
    this file. It changes through enroll and withdraw, which have rules;
    exposing it as a field would let a student PATCH themselves to
    'completed' without finishing a lesson.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    student_id: UUID
    course_id: UUID
    status: str
    enrolled_at: datetime
    # Null until the course is finished. Nothing sets it yet - completion
    # arrives with FR-14/15 in Module 3 - but it is returned now so the shape
    # does not change under clients later.
    completed_at: datetime | None


class EnrollmentListResponse(BaseModel):
    """A page of enrollments, same shape as every other list in this API."""

    items: list[EnrollmentResponse]
    total: int
    limit: int
    offset: int
