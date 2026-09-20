"""Courses, modules and lessons. See docs/SCHEMA.md sections 2, 4 and 5."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from smartcourse.infra.db.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from smartcourse.infra.db.models.user import User

# The course state machine, from UC-01:
#   draft -> publishing -> ready
#                       -> failed -> publishing
COURSE_STATUSES = ("draft", "publishing", "ready", "failed")


class Course(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "courses"

    # RESTRICT: Postgres refuses to delete a user who still has courses.
    # Cascading here would destroy every course an instructor taught, and every
    # enrollment in them, because one person was removed.
    instructor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
    )

    title: Mapped[str] = mapped_column(String(300))

    # Text rather than String: no length limit, for free-form prose.
    # `| None` makes it nullable.
    description: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="draft", index=True
    )

    # Nullable on purpose. NULL means unlimited, which is a different thing
    # from 0, which would mean full. Two meanings need two values.
    capacity: Mapped[int | None] = mapped_column(Integer)

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    instructor: Mapped["User"] = relationship(
        back_populates="courses", lazy="raise"
    )

    modules: Mapped[list["Module"]] = relationship(
        back_populates="course",
        lazy="raise",
        # Keep them in the intended order whenever they are loaded.
        order_by="Module.position",
        # Deleting a Course through the ORM deletes its modules too, matching
        # the ondelete="CASCADE" on the foreign key. Both are needed: the
        # database rule covers raw SQL, this one covers ORM deletes.
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'publishing', 'ready', 'failed')",
            name="ck_courses_status_valid",
        ),
        # Zero capacity would mean a course nobody can ever join.
        CheckConstraint("capacity IS NULL OR capacity > 0", name="ck_courses_capacity_positive"),
    )

    def __repr__(self) -> str:
        return f"<Course {self.title!r} status={self.status}>"


class Module(UUIDMixin, TimestampMixin, Base):
    """A chapter of a course."""

    __tablename__ = "modules"

    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="CASCADE"),
        index=True,
    )

    title: Mapped[str] = mapped_column(String(300))

    # A course is a sequence, not a bag. Postgres returns rows in no guaranteed
    # order unless asked, and that order can change between queries - so the
    # intended order has to be stored, or it is simply lost.
    position: Mapped[int] = mapped_column(Integer)

    course: Mapped["Course"] = relationship(back_populates="modules", lazy="raise")

    lessons: Mapped[list["Lesson"]] = relationship(
        back_populates="module",
        lazy="raise",
        order_by="Lesson.position",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # Two modules cannot both be third.
        #
        # DEFERRABLE INITIALLY DEFERRED: checked once at COMMIT rather than
        # after every statement. Reordering briefly gives two modules the same
        # position, which is fine as long as it is resolved by the end of the
        # transaction. Without this, a reorder fails halfway through.
        UniqueConstraint(
            "course_id",
            "position",
            name="uq_modules_course_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("position >= 0", name="ck_modules_position_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<Module {self.position}: {self.title!r}>"


class Lesson(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "lessons"

    module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("modules.id", ondelete="CASCADE"),
        index=True,
    )

    title: Mapped[str] = mapped_column(String(300))

    # The teaching material. This is where Part A meets Part B: UC-01 cuts it
    # into chunks, and the assistant searches those chunks in UC-05.
    content: Mapped[str | None] = mapped_column(Text)

    position: Mapped[int] = mapped_column(Integer)

    module: Mapped["Module"] = relationship(back_populates="lessons", lazy="raise")

    __table_args__ = (
        UniqueConstraint(
            "module_id",
            "position",
            name="uq_lessons_module_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("position >= 0", name="ck_lessons_position_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<Lesson {self.position}: {self.title!r}>"
