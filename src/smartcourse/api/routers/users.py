"""The signed-in user's own account.

Separate from /auth on purpose. `/auth` is about *authenticating* - proving
who you are and getting a token. `/users/me` is the user as a resource:
reading and changing it.

Everything here acts on the caller's own account. Administering other people's
accounts is a different question, with different permissions, and has no
endpoint yet - see the README.
"""

from fastapi import APIRouter

from smartcourse.api.deps import CurrentUser, UserRepo
from smartcourse.api.schemas.auth import UserResponse, UserUpdate
from smartcourse.services import auth as auth_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get(
    "/me",
    response_model=UserResponse,
    summary="The account this token belongs to",
)
async def read_me(user: CurrentUser) -> UserResponse:
    """Lets a client confirm a stored token is still good and read its roles.

    Login already returns the user, so this is for the later case: the app
    reopens tomorrow with a token and no login to piggyback on.
    """
    return UserResponse.model_validate(user)


@router.patch(
    "/me",
    response_model=UserResponse,
    summary="Edit your own profile",
)
async def update_me(
    payload: UserUpdate, user: CurrentUser, users: UserRepo
) -> UserResponse:
    """Change your name or roles. Only the fields you send.

    Adding the student role here is UC-07 - an instructor who wants to learn
    keeps one account rather than making a second.

    Sending an email gets a 422: it is the login identifier, not a profile
    field. The service decides that, not this endpoint.
    """
    updated = await auth_service.update_profile(
        users,
        user=user,
        full_name=payload.full_name,
        email=payload.email,
        roles=payload.roles,
    )
    return UserResponse.model_validate(updated)
