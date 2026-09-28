"""The users table. See docs/SCHEMA.md section 1."""

from typing import TYPE_CHECKING

from sqlalchemy import ARRAY, Boolean, CheckConstraint, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from smartcourse.infra.db.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    # Imported only for type checking, never at runtime. Course imports User
    # and User imports Course, which at runtime would be a circular import and
    # crash. The string "Course" in the annotation below is resolved later by
    # SQLAlchemy, so the real import is not needed.
    from smartcourse.infra.db.models.course import Course

# The three roles, in one place. Used by the CHECK constraint below and by
# permission checks later, so there is no second list to fall out of step.
VALID_ROLES = ("student", "instructor", "admin")


class User(UUIDMixin, TimestampMixin, Base):
    """Anyone who logs in. One person is one row, whatever they do here."""

    # The actual table name in Postgres. Without it SQLAlchemy raises an error -
    # it will not guess.
    __tablename__ = "users"

    # Mapped[str] means NOT NULL. Mapped[str | None] means nullable.
    # The type annotation is not a comment - SQLAlchemy reads it and builds the
    # column from it. This is the whole point of the 2.0 style.
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)

    # Holds a hash, never the real password. 255 is room for any modern
    # algorithm's output.
    password: Mapped[str] = mapped_column(String(255))

    full_name: Mapped[str] = mapped_column(String(200))

    # A set, not one value: an instructor can also enrol as a student.
    # PRD A-07, UC-07.
    #
    # server_default is raw SQL because it is Postgres that fills the value in,
    # so it has to be written in Postgres' own syntax rather than Python's.
    roles: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)),
        nullable=False,
        server_default=text("ARRAY['student']::varchar[]"),
    )

    # Users are deactivated, not deleted - their enrollments and courses are
    # history the analytics depend on. See the RESTRICT foreign keys elsewhere.
    #
    # Both defaults on purpose. `default` is Python-side, applied when
    # SQLAlchemy builds the row; `server_default` is Postgres' own, so an
    # INSERT from a seed script, a migration or psql also gets a value. With
    # only the Python one, any insert not going through the ORM fails on the
    # NOT NULL constraint.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    # Not a column. This is how you get at a user's courses in Python:
    # `user.courses`. SQLAlchemy works out the join from the foreign key.
    #
    # back_populates names the matching attribute on the other side, so setting
    # either one updates both without a reload.
    courses: Mapped[list["Course"]] = relationship(
        back_populates="instructor",
        # Do not silently load these when a User is fetched. Without this,
        # reading user.courses in async code raises a confusing error, because
        # loading it needs a query and there is no await here to run one.
        lazy="raise",
    )

    # Table-level rules - anything that is not about a single column.
    __table_args__ = (
        # Reject a role the application never defined. The database enforces
        # this, so a typo cannot get in through a migration, a seed script or
        # someone fixing data by hand.
        CheckConstraint(
            "roles <@ ARRAY['student','instructor','admin']::varchar[]",
            name="ck_users_roles_valid",
        ),
        # A user with no roles could do nothing and should not exist.
        #
        # cardinality(), not array_length(). array_length of an empty array is
        # NULL rather than 0, and a CHECK constraint only rejects a row when
        # its expression is explicitly FALSE - NULL passes. So the obvious
        # version silently allowed exactly the case it was written to stop.
        CheckConstraint(
            "cardinality(roles) >= 1",
            name="ck_users_roles_not_empty",
        ),
        # "Find every instructor" searches inside the array, which an ordinary
        # index cannot do. GIN is the index type for that.
        Index("ix_users_roles", "roles", postgresql_using="gin"),
    )

    def __repr__(self) -> str:
        """What you see when printing or debugging. Never the password."""
        return f"<User {self.email} roles={self.roles}>"
