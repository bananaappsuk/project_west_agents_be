from platform_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "voice_agent"
    app_key: str = "voice-agent"  # token audience this service accepts

    encryption_key: str | None = None  # Fernet key for the stored BT Cloud client secret

    agent_factory_url: str = "http://localhost:8002"
    agent_factory_internal_key: str | None = None

    cron_enabled: bool = True     # master switch for the background scheduler
    cron_tick_minutes: int = 5    # how often the scheduler checks each org's schedule
    fetch_per_run: int = 12       # recordings pulled per BT Cloud sync

    # Speech-to-text for real BT Cloud recordings (audio has no transcript). Uses the
    # OpenAI transcription API; if unset, real recordings are stored with an empty
    # transcript (the AI step then has little to work with). Demo mode needs none of this.
    openai_api_key: str | None = None
    transcribe_model: str = "whisper-1"


settings = Settings()
