"""west-agent — an application service.

Holds this app's own APIs and enforces its own RBAC via the shared SDK. The token's
audience must be this app; endpoints require app-namespaced permissions.
"""

from fastapi import Depends, FastAPI

from platform_common import Auth

from .config import settings

# audience=app_key -> tokens for other apps are rejected here.
auth = Auth(jwks_url=settings.auth_jwks_url, issuer=settings.auth_issuer, audience=settings.app_key)

app = FastAPI(title="west-agent", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "west_agent"}


@app.get("/emails")
async def list_emails(claims: dict = Depends(auth.require_scope("west-agent:emails.read"))):
    # Sample protected endpoint — real mail APIs land in the app-build phase.
    return {"org": claims.get("org"), "emails": []}
