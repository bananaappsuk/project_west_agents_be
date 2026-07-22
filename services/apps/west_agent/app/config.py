from platform_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "west_agent"
    app_key: str = "west-agent"  # the application key tokens must be scoped to (aud)


settings = Settings()
