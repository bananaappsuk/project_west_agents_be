from platform_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "auth"

    access_token_ttl: int = 900          # seconds
    refresh_token_ttl: int = 2_592_000   # seconds (30 days)

    key_id: str = "auth-key-1"
    private_key_pem: str | None = None   # PKCS8 PEM; auto-generated in dev if unset

    bootstrap_enabled: bool = True       # allow self-serve signup via /auth/register

    # Invite email delivery (optional). If SMTP_HOST is unset, invites are created but
    # not emailed — the inviter shares the one-time link manually instead.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "Project West Agent <no-reply@localhost>"
    smtp_starttls: bool = True

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host)


settings = Settings()
