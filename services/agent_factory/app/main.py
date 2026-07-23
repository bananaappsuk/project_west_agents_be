import logging
import uuid

from fastapi import FastAPI, Header, HTTPException, status

from platform_common import Auth, configure_logging

configure_logging("agent_factory")
log = logging.getLogger("agent_factory")

from . import agents  # noqa: E402,F401  (imports register agents)
from .config import settings
from .registry import REGISTRY
from .runtime import get_agent

auth = Auth(jwks_url=settings.auth_jwks_url, issuer=settings.auth_issuer)

app = FastAPI(title="Agent Factory", version="0.1.0")
log.info("agent factory ready — registered agents: %s", sorted(REGISTRY.keys()))


@app.get("/health")
async def health():
    return {"status": "ok", "service": "agent_factory", "agents": sorted(REGISTRY.keys())}


@app.get("/agents")
async def list_agents():
    return {"agents": sorted(REGISTRY.keys())}


@app.post("/agents/{app_key}/{agent_key}/invoke")
async def invoke(
    app_key: str,
    agent_key: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_internal_key: str | None = Header(default=None),
):
    # Trusted internal call (service-to-service) bypasses the user-JWT scope check.
    trusted = bool(settings.internal_api_key) and x_internal_key == settings.internal_api_key
    if not trusted:
        claims = await auth.claims(authorization)
        needed = f"{app_key}:agent.invoke"
        if needed not in (claims.get("scope") or "").split():
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing scope: {needed}")

    agent = get_agent(f"{app_key}.{agent_key}")
    if agent is None:
        log.warning("invoke: agent %s.%s not found", app_key, agent_key)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")

    # payload = {"email": {...}, "thread_id": "..."} (thread_id optional)
    email = payload.get("email", payload)
    thread_id = payload.get("thread_id") or f"{app_key}.{agent_key}:{uuid.uuid4()}"
    log.info("invoke %s.%s (trusted=%s) subject=%r", app_key, agent_key, trusted, (email or {}).get("subject"))

    try:
        result = await agent.ainvoke({"email": email}, config={"configurable": {"thread_id": thread_id}})
    except Exception as exc:  # surface LLM/graph errors clearly (e.g. bad API key)
        log.exception("invoke %s.%s FAILED", app_key, agent_key)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"agent error: {exc}") from exc

    analysis = result.get("analysis") or {}
    log.info("invoke %s.%s -> %s/%s (escalate=%s)", app_key, agent_key,
             analysis.get("category"), analysis.get("priority"), result.get("escalate"))
    return {"analysis": result.get("analysis"), "escalate": result.get("escalate")}
