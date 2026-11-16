from platform_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "voice_agent"
    app_key: str = "voice-agent"  # token audience this service accepts

    agent_factory_url: str = "http://localhost:8002"
    agent_factory_internal_key: str | None = None

    cron_enabled: bool = True     # master switch for the background scheduler
    cron_tick_minutes: int = 5    # how often the scheduler checks each org's schedule
    fetch_per_run: int = 12       # recordings pulled per BT Cloud sync

    # ── Recording source ─────────────────────────────────────────────────────
    # Configured here rather than in the database. Project West points this at one
    # phone system once; it does not change at runtime, and a settings form would
    # offer edits that either do nothing or silently disagree with the deployment.
    #
    # Everything below is resolved through `source_config.load()`, which is the
    # only place these are read. Unset means "no source configured", and the agent
    # then refuses to sync rather than falling back to demo recordings.
    #
    # Secrets belong in the platform's own secret storage (Railway variables), not
    # in a database column — which is also why nothing here is Fernet-encrypted.
    voice_source_type: str = "bt_cloud"        # "bt_cloud" | "s3"

    # BT Cloud Work / RingCentral (server-to-server JWT auth)
    rc_server_url: str = ""                    # e.g. https://platform.ringcentral.com
    rc_client_id: str = ""
    rc_client_secret: str = ""
    rc_jwt: str = ""

    # S3-compatible bucket (AWS S3, Backblaze B2, MinIO, Cloudflare R2, …)
    s3_endpoint: str = ""                      # blank = AWS default
    s3_region: str = ""
    s3_bucket: str = ""
    s3_prefix: str = ""                        # optional folder within the bucket
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    # Sync schedule, previously the voice_settings row's cron fields.
    voice_cron_frequency: str = "every6h"      # hourly | every6h | daily
    voice_cron_time: str = "02:00"             # HH:mm, used only by "daily"

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
