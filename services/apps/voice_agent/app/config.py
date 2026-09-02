from platform_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "voice_agent"
    app_key: str = "voice-agent"  # token audience this service accepts

    encryption_key: str | None = None  # Fernet key for the stored BT Cloud client secret

    agent_factory_url: str = "http://localhost:8002"
    agent_factory_internal_key: str | None = None

    # Billing entitlement checks + usage metering. Leave billing_url unset to skip
    # both — the pipeline runs unmetered (fail-open), see billing_client.py.
    billing_url: str | None = None
    billing_internal_key: str | None = None

    cron_enabled: bool = True     # master switch for the background scheduler
    cron_tick_minutes: int = 5    # how often the scheduler checks each org's schedule
    fetch_per_run: int = 12       # recordings pulled per BT Cloud sync

    # Speech-to-text for real BT Cloud recordings (audio has no transcript). Uses the
    # OpenAI transcription API; if unset, real recordings are stored with an empty
    # transcript (the AI step then has little to work with). Demo mode needs none of this.
    openai_api_key: str | None = None
    transcribe_model: str = "whisper-1"

    # CRM intake (pw-crm-be) — referrals, case communications, session change requests.
    # Off by default so environments without real CRM credentials never call out.
    crm_enabled: bool = False
    crm_base_url: str = "http://localhost:8000/api/v1"
    crm_client_id: str = ""
    crm_api_key: str = ""


settings = Settings()
