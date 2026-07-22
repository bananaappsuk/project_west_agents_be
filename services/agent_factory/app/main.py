from fastapi import Depends, FastAPI, HTTPException, status

from platform_common import Auth

from . import agents  # noqa: F401  (imports register agents)
from .config import settings
from .registry import REGISTRY
from .runtime import get_agent

auth = Auth(jwks_url=settings.auth_jwks_url, issuer=settings.auth_issuer)

app = FastAPI(title="Agent Factory", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "agent_factory", "agents": len(REGISTRY)}


@app.get("/agents")
async def list_agents():
    return {"agents": sorted(REGISTRY.keys())}


@app.post("/agents/{app_key}/{agent_key}/invoke")
async def invoke(app_key: str, agent_key: str, payload: dict, claims: dict = Depends(auth.claims)):
    # Coarse scope check: caller must hold this app's invoke permission.
    needed = f"{app_key}:agent.invoke"
    if needed not in (claims.get("scope") or "").split():
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing scope: {needed}")

    agent = get_agent(f"{app_key}.{agent_key}")
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")

    # LangGraph invocation (thread_id from the session, streaming, metering) is wired
    # when the first agent is ported under app/agents/.
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "agent runtime not yet wired")
