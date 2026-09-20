"""Every model, re-exported from one place.

This file matters more than it looks. SQLAlchemy only knows about a table once
the module defining it has been imported - and Alembic builds migrations from
that list. A model in a file nobody imports is invisible: no error, no table,
no migration. Just quietly missing.

So every new model module gets imported here, and Alembic imports this package.
One place to keep in step.
"""

from smartcourse.infra.db.base import Base
from smartcourse.infra.db.models.course import Course, Lesson, Module
from smartcourse.infra.db.models.user import User

__all__ = ["Base", "Course", "Lesson", "Module", "User"]
