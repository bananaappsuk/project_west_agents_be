from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    """Common configuration shared by every service.

    Each service subclasses this and adds its own fields.
    Values are read from environment variables / a local `.env` file.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    service_name: str = "service"
    env: str = "dev"  # dev | staging | prod

    # Identity / token verification (used by the shared Auth dependency).
    auth_issuer: str = "https://auth.platform.local"
    auth_jwks_url: str = "http://localhost:8001/.well-known/jwks.json"

    # This service's own Neon Postgres database (asyncpg URL). None if the service is stateless.
    database_url: str | None = None
