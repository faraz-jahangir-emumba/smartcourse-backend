"""Registration and login.

The use-case layer. One function per thing a person does, with the steps in
order. No HTTP here - no request, no response, no status codes - and no
queries either: those live in the repository. What is left reads as the
business steps themselves.

The same functions will be called by tests, by a seed script, and later by
background workers, none of which have a request.
"""

from smartcourse.domain.errors import (
    AccountDisabledError,
    AuthenticationError,
    ConflictError,
    ValidationError,
)
from smartcourse.infra.db.models.user import User
from smartcourse.infra.db.repositories import UserRepository
from smartcourse.infra.security import hash_password, needs_rehash, verify_password

# Roles somebody may give themselves when signing up.
#
# Admin is deliberately absent. A public endpoint that lets a caller choose
# their own role would let anyone become an admin by editing one field of the
# request - the kind of hole that looks obvious written down and is very easy
# to leave open. Admin is granted by an existing admin, never claimed.
SELF_ASSIGNABLE_ROLES = ("student", "instructor")


async def register_user(
    users: UserRepository,
    *,
    email: str,
    password: str,
    full_name: str,
    roles: list[str],
) -> User:
    """Create an account. FR-01, FR-02.

    Raises ConflictError if the email is taken, ValidationError if the roles
    are not ones a person may give themselves.
    """
    # Normalising the email matters more than it looks. Without it,
    # Sara@x.com and sara@x.com are two accounts, and the person who made the
    # second one will be certain they already registered.
    email = email.strip().lower()

    if not roles:
        raise ValidationError("At least one role is required.")

    invalid = sorted(set(roles) - set(SELF_ASSIGNABLE_ROLES))
    if invalid:
        # Say which roles are allowed as well as which were refused. An error
        # that does not tell you what to do instead is half an error.
        raise ValidationError(
            f"Cannot self-assign: {', '.join(invalid)}.",
            details={"allowed": list(SELF_ASSIGNABLE_ROLES)},
        )

    if await users.get_by_email(email) is not None:
        raise ConflictError("That email is already registered.")

    return await users.add(
        User(
            email=email,
            password=hash_password(password),
            full_name=full_name.strip(),
            # Deduplicated and ordered, so ["student","student"] does not
            # become two entries and the stored value is predictable.
            roles=sorted(set(roles)),
        )
    )


async def authenticate_user(
    users: UserRepository, *, email: str, password: str
) -> User:
    """Check credentials and return the user. FR-01.

    An unknown email and a wrong password fail identically, on purpose. A
    deactivated account is the one case that gets its own answer.
    """
    email = email.strip().lower()
    user = await users.get_by_email(email)

    if user is None:
        # An unknown email and a wrong password must be indistinguishable.
        # Otherwise the login form becomes a way to discover which addresses
        # have accounts, which is the first half of breaking into one.
        raise AuthenticationError("Incorrect email or password.")

    if not verify_password(user.password, password):
        raise AuthenticationError("Incorrect email or password.")

    if not user.is_active:
        # The one place the generic message is dropped, and only because the
        # caller has already proved the password above. Nothing is concealed
        # by pretending otherwise - it just sends a real user off to reset a
        # password that was never the problem. See AccountDisabledError.
        raise AccountDisabledError(
            "This account has been deactivated. Contact an administrator."
        )

    # The one moment the plaintext password exists. If the hashing parameters
    # have been strengthened since this password was set, upgrade it now -
    # silently, with no password reset needed.
    if needs_rehash(user.password):
        # No save call needed. `user` is tracked by the session, so changing
        # the attribute is enough - the commit at the end of the request
        # flushes it as an UPDATE. An explicit save here would imply
        # persistence depends on remembering to call it, which it does not.
        user.password = hash_password(password)

    return user
