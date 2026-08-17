from platform_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "billing"

    # Internal service-to-service key (e.g. Auth creating a trial Subscription right
    # after register()). A request bearing this key skips the platform:admin JWT check —
    # same interim pattern as Agent Factory's internal_api_key.
    internal_api_key: str | None = None

    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None

    # Base URL of the frontend, for Stripe Checkout/Portal return links.
    frontend_url: str = "http://localhost:3000"

    trial_days: int = 14


settings = Settings()
