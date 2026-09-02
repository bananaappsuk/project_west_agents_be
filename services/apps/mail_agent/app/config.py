from platform_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "mail_agent"
    app_key: str = "mail-agent"  # token audience this service accepts

    encryption_key: str | None = None  # Fernet key for mailbox passwords

    agent_factory_url: str = "http://localhost:8002"
    agent_factory_internal_key: str | None = None

    cron_enabled: bool = True     # master switch for the background scheduler
    cron_tick_minutes: int = 5    # how often the scheduler checks each org's schedule
    fetch_per_run: int = 20

    # CRM intake (pw-crm-be) — referrals, case communications, session change requests.
    # Off by default so environments without real CRM credentials never call out.
    crm_enabled: bool = False
    crm_base_url: str = "http://localhost:8000/api/v1"
    crm_client_id: str = ""
    crm_api_key: str = ""


settings = Settings()
