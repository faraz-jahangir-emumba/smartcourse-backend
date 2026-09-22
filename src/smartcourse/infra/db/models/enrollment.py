"""Enrollments and lesson progress. See docs/SCHEMA.md sections 9 and 10."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from smartcourse.infra.db.base import Base, TimestampMixin, UUIDMixin

ENROLLMENT_STATUSES = ("active", "completed", "withdrawn")


class Enrollment(UUIDMixin, TimestampMixin, Base):
    """A student joining a course. UC-02."""

    __tablename__ = "enrollments"

    # RESTRICT on both. Deleting a student or a course would erase learning
    # history the analytics in FR-19 depend on. Deactivate instead.
    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="RESTRICT"),
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="active", index=True
    )

    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Null until the course is finished. Together with enrolled_at this is the
    # whole basis of "average time to complete a course", one of the nine
    # metrics - and neither timestamp can be recovered later if not recorded
    # as it happens.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'completed', 'withdrawn')",
            name="ck_enrollments_status_valid",
        ),
        # A completed enrollment must say when, and an uncompleted one must not.
        CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL)",
            name="ck_enrollments_completed_at_matches_status",
        ),
        # The resolution of a genuine conflict between two requirements.
        #
        # FR-06 says a student cannot enrol in the same course twice, which
        # suggests UNIQUE (student_id, course_id). But FR-10 requires
        # enrollment *history*, so a student who withdraws and returns needs a
        # second row - which that constraint makes impossible.
        #
        # A partial index is unique only across rows matching its condition.
        # So: many enrollments per student and course, but only ever one
        # active. Both requirements satisfied, and enforced by the database
        # rather than trusted to application code.
        Index(
            "uq_enrollments_one_active",
            "student_id",
            "course_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    def __repr__(self) -> str:
        return f"<Enrollment student={self.student_id} course={self.course_id} {self.status}>"


class LessonProgress(UUIDMixin, Base):
    """One row each time a student finishes a lesson. UC-03.

    Three numbers come out of this one table, none of them stored: overall
    course percentage, which modules are complete, and how far through the
    current module a student is. All are counted from these rows on demand.

    Storing them instead would mean a percentage that silently goes wrong the
    moment an instructor adds a lesson. "This person finished that lesson at
    that time" stays true regardless of what changes around it.
    """

    __tablename__ = "lesson_progress"

    # Points at the enrollment, not the student. That is what makes
    # re-enrollment work: a second attempt at a course is a different
    # enrollment, so it starts with fresh progress rather than inheriting the
    # first attempt's.
    enrollment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("enrollments.id", ondelete="CASCADE"),
        index=True,
    )

    lesson_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("lessons.id", ondelete="CASCADE"),
        index=True,
    )

    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # A lesson is completed once per enrollment. Without this, clicking
        # "complete" twice creates two rows and the percentage exceeds 100.
        UniqueConstraint(
            "enrollment_id", "lesson_id", name="uq_lesson_progress_once"
        ),
    )

    def __repr__(self) -> str:
        return f"<LessonProgress enrollment={self.enrollment_id} lesson={self.lesson_id}>"
