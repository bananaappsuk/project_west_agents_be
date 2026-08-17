from platform_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "mail_agent"
    app_key: str = "mail-agent"  # token audience this service accepts

    encryption_key: str | None = None  # Fernet key for mailbox passwords

    agent_factory_url: str = "http://localhost:8002"
    agent_factory_internal_key: str | None = None

    # Billing entitlement checks + usage metering. Leave billing_url unset to skip
    # both — the pipeline runs unmetered (fail-open), see billing_client.py.
    billing_url: str | None = None
    billing_internal_key: str | None = None

    cron_enabled: bool = True     # master switch for the background scheduler
    cron_tick_minutes: int = 5    # how often the scheduler checks each org's schedule
    fetch_per_run: int = 20


settings = Settings()
