from fastapi import APIRouter

from ..config import settings
from ..keys import jwks

router = APIRouter(tags=["well-known"])


@router.get("/.well-known/jwks.json")
async def jwks_endpoint():
    return jwks()


@router.get("/.well-known/openid-configuration")
async def openid_configuration():
    issuer = settings.auth_issuer
    return {
        "issuer": issuer,
        "jwks_uri": f"{issuer}/.well-known/jwks.json",
        "token_endpoint": f"{issuer}/auth/login",
        "id_token_signing_alg_values_supported": ["RS256"],
        # OAuth2 authorization_endpoint / grant types arrive in the SSO phase.
    }
