from platform_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "agent_factory"


settings = Settings()
