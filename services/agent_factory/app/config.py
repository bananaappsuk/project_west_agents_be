from platform_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "agent_factory"

    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    confidence_threshold: float = 0.6  # below this, the agent flags escalation
    auto_send_confidence_threshold: float = 0.9  # mail agent: at/above this (and non-sensitive), a reply can auto-send

    # Internal service-to-service key (e.g. west-agent's background cron). A request
    # bearing this key skips the user-JWT scope check. Interim until client-credentials.
    internal_api_key: str | None = None


settings = Settings()
