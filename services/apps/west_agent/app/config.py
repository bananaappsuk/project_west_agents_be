from platform_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "west_agent"
    app_key: str = "west-agent"  # token audience this service accepts

    encryption_key: str | None = None  # Fernet key for mailbox passwords

    agent_factory_url: str = "http://localhost:8002"
    agent_factory_internal_key: str | None = None

    cron_enabled: bool = False
    cron_interval_minutes: int = 60
    fetch_per_run: int = 20


settings = Settings()
