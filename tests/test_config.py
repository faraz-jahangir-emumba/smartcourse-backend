"""Settings that are meant to fail. Review items #5 and #6.

Settings(...) is constructed directly here rather than through
get_settings(), which is cached and would hand back the same object every
time regardless of what was asked for.
"""

import pytest

from smartcourse.config import DEV_JWT_SECRET, Settings


def test_the_dev_secret_is_refused_outside_local() -> None:
    """The default secret is in the git history, so anyone who can read the
    repository can sign a token for any user id - and user ids are not secret
    either, since instructor_id is returned on every course.

    Refusing at startup rather than logging a warning. A warning in a log
    nobody reads is the same as no warning; a deployment that will not start
    gets noticed in minutes.
    """
    for environment in ("staging", "production"):
        with pytest.raises(ValueError, match="development default"):
            Settings(environment=environment, jwt_secret=DEV_JWT_SECRET)


def test_local_may_use_the_dev_secret() -> None:
    """Otherwise a fresh clone would not start without configuration, which
    is the thing the default exists to avoid."""
    settings = Settings(environment="local", jwt_secret=DEV_JWT_SECRET)

    assert settings.jwt_secret == DEV_JWT_SECRET


def test_a_real_secret_is_fine_anywhere() -> None:
    settings = Settings(environment="production", jwt_secret="s" * 48)

    assert settings.environment == "production"


def test_sql_echo_is_off_by_default() -> None:
    """echo=True logs bound parameters - email addresses and password hashes
    go to stdout, and from there to whatever collects it. It was hardcoded on
    while learning; the default is now off."""
    assert Settings().db_echo is False
