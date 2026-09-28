"""Password hashing and access tokens.

Infrastructure, not domain. These are technical capabilities - "turn a password
into a hash", "sign a token" - with no knowledge of what a user or a course is.
The rules about *who may do what* live in the domain layer; this module only
establishes who someone is.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from smartcourse.config import get_settings

# One hasher, reused. Creating it each time would rebuild the same
# configuration for no reason.
#
# Argon2 with its default parameters: memory-hard, so cracking it needs large
# amounts of RAM rather than just fast arithmetic. That removes the advantage
# graphics cards have, which is exactly how password cracking is done at scale.
_hasher = PasswordHasher()


class TokenError(Exception):
    """A token was missing, malformed, expired or signed with the wrong key.

    Deliberately one exception rather than several. The API must not tell a
    caller *why* their token failed - "expired" versus "bad signature" is
    information an attacker can use, and the client's action is identical
    either way: log in again.
    """


def hash_password(password: str) -> str:
    """Turn a password into a hash to store.

    The result contains the algorithm and its parameters as well as the hash
    itself, so verification later knows how it was produced. That is what makes
    it possible to strengthen the parameters without invalidating every
    existing password.
    """
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Check a password against a stored hash.

    The hash cannot be reversed. Verification re-runs the same computation on
    the supplied password and compares - which is why the original is never
    recoverable, by us or by anyone who steals the database.
    """
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        # Wrong password, or a stored value that is not an Argon2 hash at all.
        # Both mean "no", and neither is worth distinguishing to the caller.
        return False


def needs_rehash(stored_hash: str) -> bool:
    """Whether a hash was made with weaker parameters than we now use.

    Called after a successful login: if the parameters have been raised since
    this password was set, rehash it with the current ones. That upgrades
    accounts quietly as people log in, instead of requiring a password reset.
    """
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def create_access_token(user_id: UUID, roles: list[str]) -> str:
    """Mint a signed token for a user.

    The payload is base64, not encrypted - anyone holding the token can read
    it. So it carries only an id, the roles, and timestamps. Nothing secret.

    What makes it trustworthy is the signature, computed from the payload and
    the secret key. Change one character of the payload and it no longer
    matches, and forging a new signature needs the key.
    """
    settings = get_settings()
    now = datetime.now(UTC)

    payload: dict[str, Any] = {
        # "sub" (subject) is the standard claim for who the token is about.
        # Stringified because JSON has no UUID type.
        "sub": str(user_id),
        # Roles travel in the token so a permission check needs no database
        # query. The cost: a role change does not take effect until the current
        # token expires - at most an hour. Acceptable for a role that changes
        # rarely; it would not be for anything security-critical.
        "roles": roles,
        "iat": now,  # issued at
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
    }

    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Verify a token and return its contents.

    Raises TokenError for anything wrong with it. Signature and expiry are
    both checked by jwt.decode - an expired token fails here rather than being
    returned for someone downstream to remember to check.
    """
    settings = get_settings()
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            # A list, and never taken from the token itself. Reading the
            # algorithm out of the token's own header is a known attack: a
            # forged token claiming algorithm "none" would otherwise be
            # accepted without any signature at all.
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.PyJWTError as exc:
        raise TokenError("Could not validate token.") from exc
