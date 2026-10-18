"""Turning business failures into HTTP responses.

This is the only place that knows a ConflictError is a 409. Services raise
errors in the language of the problem; this module translates. Keeping the
translation in one place is what lets the same services be called later by
Celery tasks and Temporal activities, where there is no status code to return.

One envelope for every failure, so a client writes error handling once:

    {"error": {"code": "conflict", "message": "That email is already registered."}}
"""

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from smartcourse.domain.errors import (
    AccountDisabledError,
    AuthenticationError,
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)

# Unique constraints, and what it means for a caller when one is violated.
#
# Every check-then-write in this codebase has a race in it: two requests both
# look, both find nothing in the way, and both write. The database is the
# only thing that can actually prevent the second write, and it does - by
# raising IntegrityError.
#
# Without this mapping that becomes a 500: "something went wrong on our
# side", when in fact the caller did something perfectly understandable and
# simply lost a race. A 409 says so, and is retryable.
#
# Keyed by constraint name rather than by parsing the message, because the
# name is ours and stable while the message is Postgres's and is not.
# Names taken from Postgres, not guessed - two of the first six I wrote did
# not exist. Check with:
#   SELECT indexname FROM pg_indexes
#    WHERE schemaname='public' AND indexdef LIKE 'CREATE UNIQUE%';
_CONFLICT_BY_CONSTRAINT: dict[str, str] = {
    # Not uq_users_email: the column is declared unique=True, so SQLAlchemy
    # names the index ix_users_email rather than giving it a uq_ prefix.
    "ix_users_email": "That email is already registered.",
    "uq_modules_course_position": "Another module was added at the same time. Try again.",
    "uq_lessons_module_position": "Another lesson was added at the same time. Try again.",
    "uq_enrollments_one_active": "You are already enrolled in this course.",
    "uq_lesson_progress_once": "That lesson is already marked complete.",
    "uq_chunks_source_position": "That chunk already exists.",
    "course_prerequisites_pkey": "That prerequisite is already set.",
}

# The whole mapping, in one readable place.
_STATUS_BY_ERROR: dict[type[DomainError], int] = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    ValidationError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    AuthenticationError: status.HTTP_401_UNAUTHORIZED,
    AccountDisabledError: status.HTTP_403_FORBIDDEN,
    PermissionDeniedError: status.HTTP_403_FORBIDDEN,
}


def _envelope(
    *, code: str, message: str, status_code: int, details: Any = None
) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    # docs/API.md also specifies a trace_id here. It arrives in Module 3 with
    # OpenTelemetry - there is no trace to identify yet.
    return JSONResponse(status_code=status_code, content=body)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the handlers. Called once, from create_app."""

    @app.exception_handler(DomainError)
    async def _domain(_request: Request, exc: DomainError) -> JSONResponse:
        # Walk the class hierarchy so a future subclass of ConflictError maps
        # to 409 without needing its own entry.
        status_code = status.HTTP_400_BAD_REQUEST
        for error_type, code in _STATUS_BY_ERROR.items():
            if isinstance(exc, error_type):
                status_code = code
                break

        return _envelope(
            code=exc.code,
            message=exc.message,
            status_code=status_code,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Raised by FastAPI before any of our code runs - a missing field, a
        # malformed email. Reshaped into our envelope so clients do not have to
        # handle two different error formats.
        return _envelope(
            code="invalid_request",
            message="The request body or parameters are invalid.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            details=[
                {"field": ".".join(str(p) for p in e["loc"][1:]), "problem": e["msg"]}
                for e in exc.errors()
            ],
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # 404 for an unknown route, 405 for the wrong method - raised by the
        # framework, not by us.
        return _envelope(
            code="http_error",
            message=str(exc.detail),
            status_code=exc.status_code,
        )

    @app.exception_handler(IntegrityError)
    async def _integrity(_request: Request, exc: IntegrityError) -> JSONResponse:
        """A unique constraint the caller lost a race to.

        This is the safety net under every check-then-write in the services.
        The check gives a good error in the ordinary case; this gives a
        correct one when two requests arrive together and the check passed
        for both.

        A constraint we have not named is still a bug, not a conflict, so it
        falls through to the handler below rather than being reported as
        something the caller did.
        """
        name = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
        message = _CONFLICT_BY_CONSTRAINT.get(name) if name else None

        if message is None:
            return _envelope(
                code="internal_error",
                message="Something went wrong on our side.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return _envelope(
            code="conflict",
            message=message,
            status_code=status.HTTP_409_CONFLICT,
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        # Anything not anticipated is a bug, and the client learns nothing
        # about it. A stack trace or a database message in a response tells an
        # attacker about your schema and your dependencies.
        #
        # Proper logging of the detail arrives in Module 3. Today it goes to
        # stdout via uvicorn, which is enough to debug locally and not enough
        # for anything else - noted in the state file.
        return _envelope(
            code="internal_error",
            message="Something went wrong on our side.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
