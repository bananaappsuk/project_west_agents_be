from platform_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "auth"

    access_token_ttl: int = 900          # seconds
    # Absolute session length: a rotated refresh token inherits its parent's expiry
    # (see routers/auth.py's /refresh), so a sign-in lasts at most this long before
    # the frontend has to go back through Microsoft sign-in.
    refresh_token_ttl: int = 28_800      # seconds (8 hours)

    key_id: str = "auth-key-1"
    private_key_pem: str | None = None   # PKCS8 PEM; auto-generated in dev if unset

    # Microsoft Entra ID sign-in (POST /auth/entra). The frontend's SPA registration
    # also exposes the API the access token is minted for, so the token's `aud` is
    # that same client id (v2 tokens: requestedAccessTokenVersion=2).
    entra_tenant_id: str | None = None
    entra_client_id: str | None = None
    entra_scope: str = "access_as_user"
    entra_max_token_age: int = 300       # seconds; reject access tokens issued earlier than this

    # Entra security group whose members are entitled to the Voice Agent
    # ("PW-Voice-Users"), as an object id. Voice entitlement is group-based and
    # all-or-nothing by design: there are no internal Voice roles and no
    # per-recording permissions, so this one setting is the whole Voice
    # authorization model.
    #
    # Unset means NOBODY is entitled to Voice. That is deliberate: the failure
    # mode of a missing group id must be "no access" rather than "all access",
    # and before this setting existed every signed-in user received a Voice
    # token automatically.
    entra_voice_group_id: str | None = None


settings = Settings()
