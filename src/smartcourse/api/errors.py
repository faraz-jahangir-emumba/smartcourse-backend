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
