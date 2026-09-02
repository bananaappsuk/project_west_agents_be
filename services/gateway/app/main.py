"""API Gateway — one URL for the frontend; transparent reverse proxy.

Routes each request to the right upstream by longest path-prefix, forwarding the
method, query, body, and Authorization header. Each service still verifies the token
itself (JWKS), so the gateway stays thin. CORS is handled here so the browser can
call this single origin.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import logging

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from platform_common import configure_logging

from .config import settings

configure_logging("gateway")
log = logging.getLogger("gateway")

# Longest-prefix-first routing table.
ROUTES: list[tuple[str, str]] = sorted(
    [
        ("/auth", settings.auth_url),
        ("/.well-known", settings.auth_url),
        ("/admin", settings.auth_url),
        ("/common", settings.auth_url),
        ("/orgs", settings.auth_url),
        ("/emails", settings.mail_agent_url),
        ("/folders", settings.mail_agent_url),
        ("/alerts", settings.mail_agent_url),
        ("/settings", settings.mail_agent_url),
        ("/agent/", settings.mail_agent_url),   # /agent/status
        ("/voice", settings.voice_agent_url),   # all voice-agent endpoints: /voice/recordings, /voice/settings, …
        ("/agents", settings.agent_factory_url),  # /agents/{app}/{key}/invoke
    ],
    key=lambda kv: len(kv[0]),
    reverse=True,
)

# Hop-by-hop headers we must not forward.
HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "proxy-authorization", "proxy-authenticate", "te", "trailer",
    "content-encoding",
}

_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    # A bulk voice-recording sync (list + download + transcribe several files) can
    # legitimately take a few minutes — 60s was cutting those off mid-flight
    # (observed: gateway timing out and returning 502 before voice_agent had even
    # written its first row). Long enough to cover that, still a bounded ceiling.
    _client = httpx.AsyncClient(timeout=300.0)
    yield
    await _client.aclose()


app = FastAPI(title="API Gateway", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.frontend_origin.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _upstream_for(path: str) -> str | None:
    p = "/" + path
    for prefix, upstream in ROUTES:
        if p.startswith(prefix):
            return upstream
    return None


@app.get("/health")
async def health():
    return {"status": "ok", "service": "gateway", "routes": {p: u for p, u in ROUTES}}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy(path: str, request: Request):
    upstream = _upstream_for(path)
    if not upstream:
        raise HTTPException(status_code=404, detail="no route for this path")

    body = await request.body()
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}

    assert _client is not None
    try:
        upstream_resp = await _client.request(
            request.method,
            f"{upstream}/{path}",
            params=request.query_params,
            content=body,
            headers=fwd_headers,
        )
    except httpx.RequestError as exc:
        log.warning("%s /%s -> %s UNREACHABLE: %s", request.method, path, upstream, exc)
        raise HTTPException(status_code=502, detail=f"upstream unreachable: {exc}") from exc

    log.info("%s /%s -> %s [%d]", request.method, path, upstream, upstream_resp.status_code)
    resp_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in HOP_BY_HOP}
    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )
