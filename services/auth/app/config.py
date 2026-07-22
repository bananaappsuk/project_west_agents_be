from platform_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "auth"

    access_token_ttl: int = 900          # seconds
    refresh_token_ttl: int = 2_592_000   # seconds (30 days)

    key_id: str = "auth-key-1"
    private_key_pem: str | None = None   # PKCS8 PEM; auto-generated in dev if unset

    bootstrap_enabled: bool = True       # allow self-serve signup via /auth/register


settings = Settings()
