"""Registration and login shapes."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterRequest(BaseModel):
    """What a caller sends to POST /auth/register.

    Everything here is checked before any of your code runs. A missing field,
    a malformed email, a four-character password - FastAPI rejects all of them
    with a 422 and a description of what was wrong, and the service is never
    called. That is why register_user can assume it has a well-formed email and
    only worry about rules the database knows, like whether it is taken.
    """

    email: EmailStr
    password: str = Field(
        min_length=8,
        max_length=128,
        # A length floor and nothing else. Rules demanding a capital letter and
        # a symbol push people towards Password1! and are worse than length,
        # which is what actually makes guessing hard.
        #
        # The ceiling is not about security: Argon2's cost rises with input,
        # so an unbounded password field is a way to make the server do a large
        # amount of work on request.
        description="At least 8 characters.",
    )
    full_name: str = Field(min_length=1, max_length=200)
    roles: list[str] = Field(
        default=["student"],
        description="student and/or instructor. Admin cannot be self-assigned.",
    )


class LoginRequest(BaseModel):
    email: EmailStr
    # No min_length here, deliberately. Validating the length of a password
    # being *checked* would reject a short one before the credentials are
    # compared - telling the caller something about the account rather than
    # simply that the login failed.
    password: str


class TokenResponse(BaseModel):
    access_token: str
    # "bearer" is the standard scheme name: whoever bears the token may use it.
    # Clients send it back as `Authorization: Bearer <token>`.
    token_type: str = "bearer"
    # Seconds until expiry, so a client can refresh before being rejected
    # rather than discovering it through a failed request.
    expires_in: int


class UserResponse(BaseModel):
    """A user, as the API describes one.

    Note what is absent: the password hash. It is not omitted by remembering to
    leave it out - it is absent because this class lists what exists. Returning
    the model itself is what leaks; listing fields is what prevents it.
    """

    # Lets FastAPI build this straight from a SQLAlchemy object by reading
    # attributes, instead of requiring a dictionary.
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    full_name: str
    roles: list[str]
    is_active: bool
    created_at: datetime
