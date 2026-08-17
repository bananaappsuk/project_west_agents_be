from platform_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "gateway"

    # Upstream services
    auth_url: str = "http://localhost:8001"
    mail_agent_url: str = "http://localhost:8012"
    voice_agent_url: str = "http://localhost:8013"
    agent_factory_url: str = "http://localhost:8002"
    billing_url: str = "http://localhost:8003"

    # Browser origin allowed by CORS (the Next.js frontend). Comma-separated for several.
    frontend_origin: str = "http://localhost:3000"


settings = Settings()
