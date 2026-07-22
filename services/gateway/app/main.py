"""API Gateway — intentionally thin.

Today: health + a coarse token-verification demo. The reverse-proxy routing table
(and an optional edge quota check that calls Billing) is added later; business logic
and fine-grained authorization stay in the individual services.
"""

from fastapi import Depends, FastAPI

from platform_common import Auth, BaseServiceSettings

settings = BaseServiceSettings(service_name="gateway")
auth = Auth(jwks_url=settings.auth_jwks_url, issuer=settings.auth_issuer)

app = FastAPI(title="API Gateway", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "gateway"}


@app.get("/_verify")
async def verify(claims: dict = Depends(auth.claims)):
    """Coarse check the gateway performs at the edge: valid signature + not expired."""
    return {"ok": True, "sub": claims.get("sub"), "app": claims.get("aud")}
