"""Repositories - the only place that builds database queries.

A repository owns the queries for one kind of thing. Services ask it for what
they need ("the user with this email") instead of describing how to fetch it.

Three reasons that separation is worth the extra file:

- a query is written once and reused, rather than reappearing in every service
  that needs the same lookup
- a service reads as the sequence of business steps it is, with no SQL in the
  middle of it
- swapping how something is stored touches one file

Two things a repository deliberately does not do.

It does not own transactions. It flushes, never commits. Whether a request's
work is kept is decided once, in get_session, so that several repositories
used in one request either all succeed or all roll back together.

It does not decide whether an action is allowed. Ownership, state machines and
permissions are business rules and live in services. A repository that refused
to fetch a course you do not own would be making a decision it has no business
making - and the same query is needed by an admin who may.
"""

from smartcourse.infra.db.repositories.course import (
    CourseRepository,
    LessonRepository,
    ModuleRepository,
)
from smartcourse.infra.db.repositories.user import UserRepository

__all__ = [
    "CourseRepository",
    "LessonRepository",
    "ModuleRepository",
    "UserRepository",
]
