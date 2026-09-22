"""Registration endpoint. FR-01, FR-02.

Notice how little is here. The endpoint unpacks the request, calls one service,
and shapes the reply. No rules, no queries, no decisions - those live in
services and domain. If this file starts growing an `if`, the logic belongs one
layer down.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse.api.schemas.auth import RegisterRequest, UserResponse
from smartcourse.infra.db.session import get_session
from smartcourse.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])

# The session, supplied per request by FastAPI. Named once here rather than
# repeated in every signature.
SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
)
async def register(payload: RegisterRequest, session: SessionDep) -> UserResponse:
    """201 Created, and the new user - without the password hash.

    Errors are not caught here. A ConflictError from the service becomes a 409
    on its way out, handled once in api/errors.py rather than in every endpoint
    that might raise one.
    """
    user = await auth_service.register_user(
        session,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        roles=payload.roles,
    )
    return UserResponse.model_validate(user)
