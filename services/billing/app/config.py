from platform_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "billing"


settings = Settings()
