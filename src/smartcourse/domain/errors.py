"""Business failures.

These describe what went wrong in the language of the problem - "that email is
already taken" - not in the language of HTTP. There is no mention of status
codes here, because the domain does not know it is being used by a web API.

The api layer translates each of these into a status code. That translation
lives in one place, so a service can raise ConflictError without knowing or
caring that it becomes a 409.

Why this matters beyond tidiness: the same services will be called by Celery
tasks and Temporal activities in later modules, where there is no request and
no status code to return.
"""

from typing import Any


class DomainError(Exception):
    """Base for every expected business failure.

    Expected is the key word. These are outcomes the system knows about -
    duplicate email, course full, wrong password. A bug is not a DomainError;
    it is an unhandled exception, and should look like one.
    """

    code: str = "domain_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(DomainError):
    """Asked for something that does not exist."""

    code = "not_found"


class ConflictError(DomainError):
    """Would break a rule about what can exist.

    A duplicate email, enrolling twice, publishing a course that is already
    published. The request was well-formed; the current state forbids it.
    """

    code = "conflict"


class ValidationError(DomainError):
    """Broke a business rule, as opposed to being the wrong shape.

    Shape is checked before a service is ever reached - a missing field or a
    string where a number belongs is rejected by the API layer. This is for
    rules only the domain knows: a role that is not one of the three, a
    prerequisite that would close a cycle.
    """

    code = "validation_error"


class AuthenticationError(DomainError):
    """Could not establish who this is. Wrong credentials, or a bad token.

    Deliberately says nothing about which. "No account with that email" tells
    an attacker which addresses are registered, which is the first half of
    breaking into one.
    """

    code = "authentication_failed"


class PermissionDeniedError(DomainError):
    """Known who they are; they are not allowed to do this."""

    code = "permission_denied"
