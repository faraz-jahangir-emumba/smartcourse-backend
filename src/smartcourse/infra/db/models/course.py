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
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from smartcourse.infra.db.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from smartcourse.infra.db.models.user import User

# The course state machine, from UC-01:
#   draft -> publishing -> ready
#                       -> failed -> publishing
# The four states a course can be in, named once. Same reasoning as the
# roles in user.py: a mistyped status string is a silently false comparison.
STATUS_DRAFT = "draft"
STATUS_PUBLISHING = "publishing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

COURSE_STATUSES = (STATUS_DRAFT, STATUS_PUBLISHING, STATUS_READY, STATUS_FAILED)

# As a SQL literal, built from the tuple above rather than retyped. Same
# reasoning as VALID_ROLES in user.py: two hand-written copies of one list
# drift, and the drift shows up as a status Python allows and Postgres
# rejects.
_COURSE_STATUSES_SQL = ", ".join(f"'{status}'" for status in COURSE_STATUSES)


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
        String(20), nullable=False, server_default=STATUS_DRAFT, index=True
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

    # Two relationships to the same table, in opposite directions.
    #
    # CoursePrerequisite has two foreign keys to courses, so SQLAlchemy cannot
    # work out which one each relationship should follow - it refuses rather
    # than guessing. foreign_keys spells it out.
    #
    # The link rows themselves - "what must I finish before taking this
    # course?" Named _links because the thing most code actually wants is the
    # courses, not the join rows, and that name should belong to the useful one.
    prerequisite_links: Mapped[list["CoursePrerequisite"]] = relationship(
        back_populates="course",
        lazy="raise",
        foreign_keys="CoursePrerequisite.course_id",
        cascade="all, delete-orphan",
    )

    # The same thing, as courses, reading straight through the join table.
    #
    # viewonly because there are now two ways to see one set of rows, and
    # letting both write them means two sources of truth about the same data.
    # Writing goes through CourseRepository.replace_prerequisites; this is for
    # reading, which is what an API response needs.
    prerequisites: Mapped[list["Course"]] = relationship(
        "Course",
        secondary="course_prerequisites",
        primaryjoin="Course.id == CoursePrerequisite.course_id",
        secondaryjoin="Course.id == CoursePrerequisite.prerequisite_course_id",
        viewonly=True,
        lazy="raise",
        order_by="Course.title",
    )

    # "which courses require this one?" - the reason this course cannot simply
    # be deleted.
    required_by: Mapped[list["CoursePrerequisite"]] = relationship(
        back_populates="prerequisite_course",
        lazy="raise",
        foreign_keys="CoursePrerequisite.prerequisite_course_id",
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({_COURSE_STATUSES_SQL})",
            name="ck_courses_status_valid",
        ),
        # Zero capacity would mean a course nobody can ever join.
        CheckConstraint("capacity IS NULL OR capacity > 0", name="ck_courses_capacity_positive"),
    )

    def __repr__(self) -> str:
        return f"<Course {self.title!r} status={self.status}>"


class CoursePrerequisite(Base):
    """Course A requires course B to be completed first. FR-08.

    One row per requirement, so React requiring HTML, CSS and JavaScript is
    three rows. An array of ids on `courses` would have been simpler to read
    and much worse: Postgres cannot enforce a foreign key on array *elements*,
    so deleting a prerequisite course would succeed and leave every course that
    required it pointing at nothing.

    No UUIDMixin here. The pair of columns is the primary key, which also stops
    the same prerequisite being added twice - a surrogate id would allow
    duplicates unless a unique constraint were added anyway.
    """

    __tablename__ = "course_prerequisites"

    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # RESTRICT, where the column above is CASCADE. Same table, opposite rules.
    #
    # Deleting a course should take its own list of requirements with it
    # (CASCADE), but must not be allowed while other courses depend on it
    # (RESTRICT) - that would silently change who can enrol on them.
    prerequisite_course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="RESTRICT"),
        primary_key=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    course: Mapped["Course"] = relationship(
        back_populates="prerequisite_links",
        lazy="raise",
        foreign_keys=[course_id],
    )
    prerequisite_course: Mapped["Course"] = relationship(
        back_populates="required_by",
        lazy="raise",
        foreign_keys=[prerequisite_course_id],
    )

    __table_args__ = (
        # A course requiring itself would be permanently unenrollable.
        #
        # This catches one hop only. Longer cycles - A needs B, B needs C, C
        # needs A - are spread across three rows, and a CHECK sees one row at a
        # time. Those are rejected in the service layer with a recursive query.
        # FR-08a.
        CheckConstraint(
            "course_id <> prerequisite_course_id",
            name="ck_course_prerequisites_not_self",
        ),
    )

    def __repr__(self) -> str:
        return f"<CoursePrerequisite {self.course_id} needs {self.prerequisite_course_id}>"


class Module(UUIDMixin, TimestampMixin, Base):
    """A chapter of a course."""

    __tablename__ = "modules"

    # No index=True. uq_modules_course_position is a unique index on
    # (course_id, position), and a btree index on (a, b) already answers queries
    # that filter on `a` alone - the rows for one course sit together in
    # it. A second index on course_id would be a duplicate: a write on
    # every insert and update, and never chosen by the planner.
    # Flagged in the Week 1 review.
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="CASCADE"),
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

    # No index=True. uq_lessons_module_position is a unique index on
    # (module_id, position), and a btree index on (a, b) already answers queries
    # that filter on `a` alone - the rows for one module sit together in
    # it. A second index on module_id would be a duplicate: a write on
    # every insert and update, and never chosen by the planner.
    # Flagged in the Week 1 review.
    module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("modules.id", ondelete="CASCADE"),
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
