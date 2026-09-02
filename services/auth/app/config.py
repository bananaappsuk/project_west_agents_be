from platform_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "auth"

    access_token_ttl: int = 900          # seconds
    refresh_token_ttl: int = 2_592_000   # seconds (30 days)

    key_id: str = "auth-key-1"
    private_key_pem: str | None = None   # PKCS8 PEM; auto-generated in dev if unset

    # White-label display name — used as the invite email's "From" name when
    # SMTP_FROM isn't set explicitly. Keep in sync with the frontend's
    # NEXT_PUBLIC_APP_NAME for one consistent brand across the deployment.
    app_name: str = "AI Agent Platform"

    # Invite email delivery (optional). If SMTP_HOST is unset, invites are created but
    # not emailed — the inviter shares the one-time link manually instead.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None         # e.g. "Acme Agent <no-reply@acme.com>"
    smtp_starttls: bool = True

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host)

    @property
    def smtp_from_header(self) -> str:
        return self.smtp_from or f"{self.app_name} <no-reply@localhost>"


settings = Settings()
