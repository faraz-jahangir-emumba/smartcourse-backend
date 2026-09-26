"""Repositories - the only place that builds database queries.

A repository owns the queries for one kind of thing. Services ask it for what
they need ("the user with this email") instead of describing how to fetch it.

Three reasons that separation is worth the extra file:

- a query is written once and reused, rather than reappearing in every service
  that needs the same lookup
- a service reads as the sequence of business steps it is, with no SQL in the
  middle of it
- swapping how something is stored touches one file

What this deliberately does not do is own transactions. A repository flushes,
never commits. Whether a request's work is kept is decided once, in
get_session, so that several repositories used in one request either all
succeed or all roll back together.
"""

from smartcourse.infra.db.repositories.user import UserRepository

__all__ = ["UserRepository"]
