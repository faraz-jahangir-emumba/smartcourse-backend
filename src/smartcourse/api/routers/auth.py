"""Registration, login, and who-am-I. FR-01, FR-02.

Notice how little is here. Each endpoint unpacks the request, calls one
service, and shapes the reply. No rules, no queries, no decisions - those live
in services and domain. If this file grows an `if`, the logic belongs one
layer down.
"""

from fastapi import APIRouter, status

from smartcourse.api.deps import CurrentUser, UserRepo
from smartcourse.api.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from smartcourse.config import get_settings
from smartcourse.infra.security import create_access_token
from smartcourse.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
)
async def register(payload: RegisterRequest, users: UserRepo) -> UserResponse:
    """201 Created, and the new user - without the password hash.

    Errors are not caught here. A ConflictError from the service becomes a 409
    on its way out, handled once in api/errors.py rather than in every
    endpoint that might raise one.
    """
    user = await auth_service.register_user(
        users,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        roles=payload.roles,
    )
    return UserResponse.model_validate(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange credentials for a token",
)
async def login(payload: LoginRequest, users: UserRepo) -> TokenResponse:
    """200 with a token, or 401 saying nothing useful to an attacker.

    Roles travel in the token, so a permission check needs no extra query. The
    cost is that a role change does not take effect until the token expires -
    at most an hour.
    """
    user = await auth_service.authenticate_user(
        users, email=payload.email, password=payload.password
    )
    settings = get_settings()
    return TokenResponse(
        access_token=create_access_token(user.id, user.roles),
        expires_in=settings.access_token_ttl_minutes * 60,
    )


@router.get(
    "/me",
    response_model=UserResponse,
    summary="The account this token belongs to",
)
async def me(user: CurrentUser) -> UserResponse:
    """Lets a client confirm a token is still good, and read its own roles.

    Also the smallest possible check that authentication works end to end.
    """
    return UserResponse.model_validate(user)
