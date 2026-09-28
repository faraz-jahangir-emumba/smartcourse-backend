"""Dependencies every protected endpoint shares.

Two jobs, kept separate on purpose:

- get_current_user answers *who is this*
- require_roles answers *may they do this*

Authentication and authorisation are different questions. Confusing them is
how endpoints end up half-protected: something checks a token is valid and
nobody checks the holder is allowed.
"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse.domain.errors import AuthenticationError, PermissionDeniedError
from smartcourse.infra.db.models.user import User
from smartcourse.infra.db.repositories import (
    CourseRepository,
    LessonRepository,
    ModuleRepository,
    UserRepository,
)
from smartcourse.infra.db.session import get_session
from smartcourse.infra.security import TokenError, decode_access_token

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_user_repository(session: SessionDep) -> UserRepository:
    """Build a UserRepository on the request's session.

    Constructed here rather than inside each service, so every repository used
    during one request shares a transaction. A service that built its own
    would get a separate one, and two writes in a single request could no
    longer succeed or fail together.
    """
    return UserRepository(session)


UserRepo = Annotated[UserRepository, Depends(get_user_repository)]


def get_course_repository(session: SessionDep) -> CourseRepository:
    return CourseRepository(session)


def get_module_repository(session: SessionDep) -> ModuleRepository:
    return ModuleRepository(session)


def get_lesson_repository(session: SessionDep) -> LessonRepository:
    return LessonRepository(session)


# All three resolve to the same session within one request - FastAPI calls
# each dependency once and reuses the result - so an endpoint touching a
# course and its modules works in a single transaction.
CourseRepo = Annotated[CourseRepository, Depends(get_course_repository)]
ModuleRepo = Annotated[ModuleRepository, Depends(get_module_repository)]
LessonRepo = Annotated[LessonRepository, Depends(get_lesson_repository)]

# Reads the Authorization header and expects `Bearer <token>`.
#
# auto_error=False so a missing header reaches our own code rather than
# producing FastAPI's default error, which would not match the envelope every
# other failure uses.
_bearer = HTTPBearer(auto_error=False, description="Bearer <token from /auth/login>")


async def get_current_user(
    users: UserRepo,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """Who is making this request.

    The token alone would be enough to know the user id and roles - that is
    the point of putting them in it. The database is still consulted, for one
    reason: a token stays valid until it expires, so without this check a
    deactivated user would keep working for up to an hour.

    That costs one query per request. Worth it; "we cannot switch anyone off"
    is not an acceptable property.
    """
    if credentials is None:
        raise AuthenticationError("Not authenticated.")

    try:
        payload = decode_access_token(credentials.credentials)
    except TokenError as exc:
        raise AuthenticationError("Invalid or expired token.") from exc

    subject = payload.get("sub")
    if not subject:
        raise AuthenticationError("Invalid or expired token.")

    try:
        user_id = UUID(subject)
    except ValueError as exc:
        # A well-signed token whose subject is not a UUID means someone is
        # minting tokens with our key, or the format changed. Either way it is
        # not a valid identity.
        raise AuthenticationError("Invalid or expired token.") from exc

    user = await users.get_by_id(user_id)
    if user is None or not user.is_active:
        # A deleted or deactivated account, so the token is simply no longer
        # valid. 401 rather than the 403 that login gives for a disabled
        # account: there, the caller had just proved the password. Here the
        # holder of a token may be anyone.
        raise AuthenticationError("Invalid or expired token.")

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*allowed: str):  # noqa: ANN201 - returns a FastAPI dependency
    """Build a dependency that admits only these roles. FR-03.

    Used as:

        @router.post("/courses", dependencies=[Depends(require_roles("instructor"))])

    A user needs *any one* of the roles listed, not all of them - roles are a
    set, and an instructor who is also a student should still be able to
    author courses.

    Admin is not special-cased here. If admins should reach an endpoint, the
    endpoint says so. Implicit "admin can do everything" is how a permission
    system stops being reviewable: you can no longer tell what an admin may do
    by reading the routes.
    """

    async def check(user: CurrentUser) -> User:
        if not set(allowed) & set(user.roles):
            raise PermissionDeniedError(
                f"This requires one of: {', '.join(sorted(allowed))}.",
                details={"your_roles": sorted(user.roles)},
            )
        return user

    return check
