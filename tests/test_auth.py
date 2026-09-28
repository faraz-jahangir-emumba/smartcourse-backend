"""Registration, login and identity. FR-01, FR-02.

Each test states one fact about the system. The names are written so a failure
report reads as a sentence about what broke.
"""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse.infra.db.models.user import User

from tests.conftest import PASSWORD, auth_header, register


async def test_register_returns_the_new_user(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/register",
        json={
            "email": "new@example.com",
            "password": PASSWORD,
            "full_name": "New Person",
            "roles": ["student"],
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "new@example.com"
    assert body["roles"] == ["student"]
    assert body["is_active"] is True


async def test_register_never_returns_the_password(client: AsyncClient) -> None:
    """The response model lists what exists, so the hash cannot leak.

    Worth its own test because it is a silent failure: nothing breaks if it
    regresses, the hash simply starts appearing in responses.
    """
    body = await register(client, "secret@example.com")

    assert "password" not in body
    assert not any("password" in key for key in body)


async def test_email_is_normalised(client: AsyncClient) -> None:
    """Otherwise Sara@ and sara@ are two accounts."""
    body = await register(client, "MiXeD@Example.COM")

    assert body["email"] == "mixed@example.com"


async def test_duplicate_email_is_rejected(client: AsyncClient) -> None:
    await register(client, "taken@example.com")

    response = await client.post(
        "/auth/register",
        json={
            "email": "taken@example.com",
            "password": PASSWORD,
            "full_name": "Imposter",
            "roles": ["student"],
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_duplicate_check_ignores_case(client: AsyncClient) -> None:
    """The normalisation has to happen before the uniqueness check, not after.

    Getting this wrong is easy and the symptom is two accounts for one person.
    """
    await register(client, "case@example.com")

    response = await client.post(
        "/auth/register",
        json={
            "email": "CASE@EXAMPLE.COM",
            "password": PASSWORD,
            "full_name": "Same Person",
            "roles": ["student"],
        },
    )

    assert response.status_code == 409


async def test_admin_cannot_be_self_assigned(client: AsyncClient) -> None:
    """A public endpoint where the caller picks their own role would let
    anyone become an admin by editing one field."""
    response = await client.post(
        "/auth/register",
        json={
            "email": "root@example.com",
            "password": PASSWORD,
            "full_name": "Root",
            "roles": ["admin"],
        },
    )

    assert response.status_code == 422
    body = response.json()["error"]
    assert "admin" in body["message"]
    # The error says what is allowed, not only what was refused.
    assert body["details"]["allowed"] == ["student", "instructor"]


async def test_short_password_is_rejected_before_the_service_runs(
    client: AsyncClient,
) -> None:
    """Caught by the schema, which is why the code differs from the domain
    errors above."""
    response = await client.post(
        "/auth/register",
        json={
            "email": "short@example.com",
            "password": "abc",
            "full_name": "Short",
            "roles": ["student"],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


async def test_login_returns_a_usable_token(client: AsyncClient) -> None:
    await register(client, "login@example.com")

    response = await client.post(
        "/auth/login", json={"email": "login@example.com", "password": PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    assert body["access_token"].count(".") == 2  # header.payload.signature


async def test_wrong_password_and_unknown_email_are_indistinguishable(
    client: AsyncClient,
) -> None:
    """The most important test in this file.

    If the two differed, the login form would become a way to discover which
    addresses have accounts - the first half of breaking into one.
    """
    await register(client, "real@example.com")

    wrong_password = await client.post(
        "/auth/login", json={"email": "real@example.com", "password": "not-it-at-all"}
    )
    unknown_email = await client.post(
        "/auth/login", json={"email": "ghost@example.com", "password": PASSWORD}
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


async def test_users_me_returns_the_token_holder(client: AsyncClient) -> None:
    await register(client, "whoami@example.com", ["instructor"])
    headers = await auth_header(client, "whoami@example.com")

    response = await client.get("/users/me", headers=headers)

    assert response.status_code == 200
    assert response.json()["email"] == "whoami@example.com"
    assert response.json()["roles"] == ["instructor"]


async def test_users_me_without_a_token_is_rejected(client: AsyncClient) -> None:
    response = await client.get("/users/me")

    assert response.status_code == 401


async def test_users_me_with_a_tampered_token_is_rejected(client: AsyncClient) -> None:
    """Changing the payload breaks the signature, which is the whole basis of
    trusting a token whose contents anyone can read."""
    await register(client, "tamper@example.com")
    headers = await auth_header(client, "tamper@example.com")
    headers["Authorization"] = headers["Authorization"][:-4] + "AAAA"

    response = await client.get("/users/me", headers=headers)

    assert response.status_code == 401


async def test_disabled_account_is_told_so(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The one case where the generic message is dropped.

    By this point the password has already been verified, so nothing is
    concealed by pretending the credentials were wrong - it only sends a real
    user off to reset a password that was never the problem.

    403 rather than 401: 401 means "we do not know who you are", and here we
    do. We know exactly who they are and they still may not in.

    The user is deactivated directly through the session because no endpoint
    does it yet - user administration is not part of Module 1.
    """
    await register(client, "disabled@example.com")
    user = await session.scalar(
        select(User).where(User.email == "disabled@example.com")
    )
    user.is_active = False
    await session.flush()

    response = await client.post(
        "/auth/login",
        json={"email": "disabled@example.com", "password": PASSWORD},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "account_disabled"


async def test_disabled_account_still_fails_generically_on_a_wrong_password(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Order matters. The password is checked *before* the active flag, so a
    wrong password on a disabled account reveals nothing about either."""
    await register(client, "disabled2@example.com")
    user = await session.scalar(
        select(User).where(User.email == "disabled2@example.com")
    )
    user.is_active = False
    await session.flush()

    response = await client.post(
        "/auth/login",
        json={"email": "disabled2@example.com", "password": "wrong-password"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"


async def test_login_returns_the_user_alongside_the_token(
    client: AsyncClient,
) -> None:
    """Login has already loaded the user to check the password, so returning
    it saves the client an immediate second call to /auth/me.

    And the same rule as everywhere else: no password hash."""
    await register(client, "withuser@example.com", ["instructor"])

    response = await client.post(
        "/auth/login", json={"email": "withuser@example.com", "password": PASSWORD}
    )

    assert response.status_code == 200
    user = response.json()["user"]
    assert user["email"] == "withuser@example.com"
    assert user["roles"] == ["instructor"]
    assert "password" not in user


# --- editing your own profile --- FR-03a, UC-07 ---------------------------


async def test_patch_only_changes_what_was_sent(client: AsyncClient) -> None:
    """An omitted field keeps its value - that is what PATCH means."""
    await register(client, "edit@example.com", ["student"])
    headers = await auth_header(client, "edit@example.com")

    response = await client.patch(
        "/users/me", json={"full_name": "New Name"}, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["full_name"] == "New Name"
    assert response.json()["email"] == "edit@example.com"  # untouched
    assert response.json()["roles"] == ["student"]         # untouched


async def test_an_instructor_can_become_a_student_too(
    client: AsyncClient,
) -> None:
    """UC-07. One account gaining a role, not a second account."""
    await register(client, "teach@example.com", ["instructor"])
    headers = await auth_header(client, "teach@example.com")

    response = await client.patch(
        "/users/me",
        json={"roles": ["instructor", "student"]},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["roles"] == ["instructor", "student"]


async def test_admin_still_cannot_be_self_assigned(client: AsyncClient) -> None:
    """Allowing it here would move the hole rather than close it: register as
    a student, then promote yourself."""
    await register(client, "climber@example.com", ["student"])
    headers = await auth_header(client, "climber@example.com")

    response = await client.patch(
        "/users/me", json={"roles": ["admin"]}, headers=headers
    )

    assert response.status_code == 422
    assert response.json()["error"]["details"]["allowed"] == [
        "student",
        "instructor",
    ]


async def test_email_cannot_be_changed(client: AsyncClient) -> None:
    """Refused rather than ignored, so the caller is not told it worked."""
    await register(client, "fixed@example.com")
    headers = await auth_header(client, "fixed@example.com")

    response = await client.patch(
        "/users/me", json={"email": "new@example.com"}, headers=headers
    )

    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Email cannot be changed."


async def test_a_rejected_email_leaves_the_rest_of_the_request_alone(
    client: AsyncClient,
) -> None:
    """The refusal has to happen before anything is written.

    Otherwise a request carrying both fields would half-succeed: the name
    changed, the call returned 422, and the caller has no way to know which
    part landed.
    """
    await register(client, "both@example.com")
    headers = await auth_header(client, "both@example.com")

    response = await client.patch(
        "/users/me",
        json={"full_name": "New Name", "email": "new@example.com"},
        headers=headers,
    )
    assert response.status_code == 422

    after = await client.get("/users/me", headers=headers)
    assert after.json()["full_name"] != "New Name"
    assert after.json()["email"] == "both@example.com"


async def test_empty_roles_are_rejected(client: AsyncClient) -> None:
    """A user with no roles could do nothing and should not exist."""
    await register(client, "noroles@example.com")
    headers = await auth_header(client, "noroles@example.com")

    response = await client.patch("/users/me", json={"roles": []}, headers=headers)

    assert response.status_code == 422


async def test_editing_a_profile_requires_a_token(client: AsyncClient) -> None:
    response = await client.patch("/users/me", json={"full_name": "Nobody"})

    assert response.status_code == 401
