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


class UserResponse(BaseModel):
    """A user, as the API describes one.

    Note what is absent: the password hash. It is not omitted by remembering to
    leave it out - it is absent because this class lists what exists. Returning
    the model itself is what leaks; listing fields is what prevents it.

    Defined above TokenResponse because that one embeds it, and a class cannot
    reference a name defined further down the file.
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


class UserUpdate(BaseModel):
    """Changing your own profile. Every field optional.

    Omitting a field leaves it alone - that is what PATCH means, and it is
    why a name cannot be wiped by a request that simply did not mention it.

    Password is deliberately absent. Changing one has to prove you know the
    current one, or anybody holding a stolen token could lock the real owner
    out permanently. That needs its own endpoint and its own input.

    is_active is absent too. Deactivating yourself is closing an account, not
    editing a field - and it is one-way, since an inactive user cannot log in
    to undo it.

    email is listed but always refused, which is deliberate. Silently dropping
    it would hand the caller a 200 and no hint that nothing changed; listing
    it and answering 422 tells them why. The field exists to carry that
    message, not to be accepted.
    """

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    email: EmailStr | None = Field(
        default=None,
        description="Rejected with 422. Email is the login identifier.",
    )
    roles: list[str] | None = None


class TokenResponse(BaseModel):
    access_token: str
    # "bearer" is the standard scheme name: whoever bears the token may use it.
    # Clients send it back as `Authorization: Bearer <token>`.
    token_type: str = "bearer"
    # Seconds until expiry, so a client can refresh before being rejected
    # rather than discovering it through a failed request.
    expires_in: int
    # The account that just signed in.
    #
    # Login has already loaded this user to check the password, so returning
    # it costs nothing and saves the client an immediate second call to find
    # out who it is and what they may do.
    #
    # /auth/me still exists and is not redundant: it answers the same question
    # later, from a stored token, when the app reopens.
    user: UserResponse
