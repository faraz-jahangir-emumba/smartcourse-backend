"""Every model, re-exported from one place.

This file matters more than it looks. SQLAlchemy only knows about a table once
the module defining it has been imported - and Alembic builds migrations from
that list. A model in a file nobody imports is invisible: no error, no table,
no migration. Just quietly missing.

So every new model module gets imported here, and Alembic imports this package.
One place to keep in step.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import column_property

from smartcourse.infra.db.base import Base
from smartcourse.infra.db.models.content import Asset, Chunk
from smartcourse.infra.db.models.course import (
    Course,
    CoursePrerequisite,
    Lesson,
    Module,
)
from smartcourse.infra.db.models.enrollment import (
    ENROLLMENT_ACTIVE,
    Enrollment,
    LessonProgress,
)
from smartcourse.infra.db.models.event import FailedEvent
from smartcourse.infra.db.models.user import User

# How many people are currently on each course. FR-07, and what a catalogue
# needs to show "1 place left" rather than making somebody try to enrol and
# read the 409.
#
# Defined here rather than on the Course class because it reads the
# enrollments table, and course.py cannot import enrollment.py - enrollment
# already imports from course. Attaching it once both modules are loaded is
# SQLAlchemy's answer to that, and this file is the one place guaranteed to
# have seen both.
#
# A correlated subquery, so a listing of twenty courses is still one round
# trip. The alternative - counting per course in the service - is the N+1
# that selectinload exists to avoid, twenty queries where one will do.
#
# Not deferred, so it is always present. The same reasoning as the generated
# search_vector column: a derived value that something has to remember to
# ask for is a derived value that will be forgotten, and the cost here is an
# index scan on enrollments.course_id. Only *active* enrolments count -
# withdrawn ones are history and do not occupy a seat, which is the same
# rule the capacity check uses.
Course.enrolled_count = column_property(
    select(func.count(Enrollment.id))
    .where(
        Enrollment.course_id == Course.id,
        Enrollment.status == ENROLLMENT_ACTIVE,
    )
    .correlate_except(Enrollment)
    .scalar_subquery()
)

__all__ = [
    "Asset",
    "Base",
    "Chunk",
    "Course",
    "CoursePrerequisite",
    "Enrollment",
    "FailedEvent",
    "Lesson",
    "LessonProgress",
    "Module",
    "User",
]
