"""Application settings.

Everything configurable is declared here once and read from the environment.
No other module reaches for os.environ, so there is exactly one place to look
for what this application can be told.

Pydantic validates at startup. A missing or malformed setting fails when the
process boots, not at 2am on the first request that happens to need it.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Ignore variables we do not declare. Without this, any unrelated
        # environment variable would be an error.
        extra="ignore",
        case_sensitive=False,
    )

    # --- security ---------------------------------------------------------
    # The key every token is signed with. Anyone holding it can mint a token
    # for any user, so in a real deployment this comes from the environment and
    # never has a default. The default here is explicitly marked as unsafe so
    # it cannot be mistaken for a real one.
    # At least 32 bytes: HMAC-SHA256 is defined for keys of at least its own
    # output size, and PyJWT warns below that. This default is long enough to
    # be valid and obviously fake so it cannot be mistaken for a real key.
    jwt_secret: str = "dev-only-insecure-secret-do-not-use-in-any-real-deployment"
    jwt_algorithm: str = "HS256"

    # PRD A-01: one hour, no refresh flow. Short because a token cannot be
    # revoked - it stays valid until it expires, even if the user is deleted.
    access_token_ttl_minutes: int = 60

    # --- postgres ---------------------------------------------------------
    # Defaults match compose.yml, so the app runs with no .env at all.
    # Real deployments override them; there are no secrets here to leak.
    postgres_user: str = "smartcourse"
    postgres_password: str = "smartcourse"
    postgres_db: str = "smartcourse"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    @property
    def database_url(self) -> str:
        """Connection string for SQLAlchemy.

        Built from the parts rather than stored whole, because the parts get
        reused and only one of them changes between environments. Running the
        app inside Docker changes the host from localhost to postgres and
        nothing else.

        The `+asyncpg` suffix tells SQLAlchemy which driver to use. Same
        database, different driver, different prefix.
        """
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """The settings object. Use this rather than constructing Settings().

    lru_cache makes it a singleton: the environment is read and validated once,
    and every caller gets the same object. Without it, each call would re-read
    and re-validate for no reason.
    """
    return Settings()
