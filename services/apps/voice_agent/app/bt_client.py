"""BT Cloud Work client — fetches call recordings for an organisation.

BT Cloud Work is a white-labelled RingCentral platform, so this speaks the RingCentral
REST API:

  1. Auth  — JWT server-to-server grant:
       POST {server}/restapi/oauth/token   (Basic clientId:clientSecret, jwt-bearer assertion)
  2. List  — calls that have recordings:
       GET  {server}/restapi/v1.0/account/~/call-log?withRecording=true&view=Detailed
  3. Audio — download each recording:
       GET  {recording.contentUri}          (Bearer token)
  4. Text  — recordings are AUDIO ONLY, so we transcribe with the OpenAI transcription
             API (VOICE OPENAI_API_KEY) before the Agent Factory analyses the transcript.

If no JWT credential is configured for the org, `list_latest` returns realistic **demo**
recordings so the whole pipeline runs without a BT account (mirrors the Agent Factory's
"runs without a paid key" philosophy). Configure the JWT in Settings to go live.

Note: RingCentral retired its standalone developer *sandbox* (Dec 2024) and call recording
is not available on sandbox in Europe — so real test data means a real BT Cloud Work account
where recordings exist (make + record a test call). Docs:
  https://developers.ringcentral.com/guide/basics/partners/bt
"""

from __future__ import annotations

import base64
import logging
import random
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

from .transcribe import transcribe

log = logging.getLogger("voice_agent.bt_client")

# ─────────────────────────────────────────────────────────────────────────────
# Access-token cache — this deployment has one BT Cloud Work account (no multi-
# org feature), so a single cached RingCentral OAuth token is reused across a
# sync's listing call, every transcription in that sync, and any later
# on-demand audio playback request, instead of minting a fresh token per call.
# Safe as a module-level variable: this service runs single-process (no
# --workers, see run-all.ps1).
# ─────────────────────────────────────────────────────────────────────────────
_token_cache: tuple[str, float] | None = None  # (access_token, expires_at_epoch)
_TOKEN_EXPIRY_BUFFER = 60.0  # seconds of headroom before a cached token is treated as expired


def _get_token(server: str, client_id: str, client_secret: str, jwt: str, *, force: bool = False) -> str:
    global _token_cache
    if not force and _token_cache and _token_cache[1] - _TOKEN_EXPIRY_BUFFER > time.time():
        return _token_cache[0]
    token, expires_in = _access_token(server, client_id, client_secret, jwt)
    _token_cache = (token, time.time() + expires_in)
    return token

# ─────────────────────────────────────────────────────────────────────────────
# Demo data (used until a JWT credential is configured)
# ─────────────────────────────────────────────────────────────────────────────

_CALLERS = [
    "Priya Anand", "Daniel Osei", "Fatima Rahman", "Marcus Webb", "Grace Lin",
    "Oliver Bennett", "Sophie Carter", "Ben Foster", "Nadia Hussain", "Harriet Moss",
    "Leo Martins", "Amara Okafor", "Unknown Caller",
]
_AGENTS = ["Sam Wills", "Ella Norton", "Tariq Hussain"]
_SCENARIOS = [
    "this is the second time your engineer hasn't shown up. I took the afternoon off work for nothing. I'm ready to cancel...",
    "I saw the business fibre plan online, how quickly could that get installed if I sign up this week?",
    "just wanted to check what time you're open until on Saturdays. Perfect, thank you.",
    "I've got two charges of the same amount on the same day, that can't be right. I need this sorted before my next payment.",
    "ok it's back on now, all the lights are green. Thanks for sorting that out.",
    "Tuesday the 4th works well for us, anytime after 10 is fine. Could you text me a reminder the day before?",
    "that's worked, I can hear the new greeting now, brilliant, thank you.",
    "every day around 2pm the line just cuts out mid-call, it's affecting how I run my business.",
    "that's helpful, let me speak to my business partner and we'll call back next week.",
    "ok that makes more sense now you've broken it down, I understand the increase.",
    "nobody turned up and nobody called to say why, I've wasted a whole morning.",
]


def _rand_phone() -> str:
    return f"+44 7700 9{random.randint(0, 9)}{random.randint(1000, 9999):04d}"


def _rand_duration() -> str:
    return f"{random.randint(1, 8)}:{random.randint(0, 59):02d}"


def _fetch_demo(count: int) -> list[dict]:
    today = date.today()
    out: list[dict] = []
    for _ in range(count):
        d = today - timedelta(days=random.randint(0, 3))
        out.append({
            "ext_id": f"btc-{uuid.uuid4().hex[:12]}",
            "caller": random.choice(_CALLERS),
            "phone": _rand_phone(),
            "agent": random.choice(_AGENTS),
            "date": d.isoformat(),
            "duration": _rand_duration(),
            "transcript": '"...' + random.choice(_SCENARIOS) + '"',
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Real RingCentral / BT Cloud Work path
# ─────────────────────────────────────────────────────────────────────────────

def _server(endpoint: str) -> str:
    """Normalise the configured endpoint to a scheme://host base (drops any path)."""
    parts = urlsplit(endpoint if "://" in endpoint else f"https://{endpoint}")
    if not parts.netloc:
        raise ValueError("endpoint must be a RingCentral/BT Cloud Work server URL, e.g. https://platform.ringcentral.com")
    return f"{parts.scheme}://{parts.netloc}"


def _access_token(server: str, client_id: str, client_secret: str, jwt: str) -> tuple[str, int]:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    with httpx.Client(timeout=30.0) as c:
        r = c.post(
            f"{server}/restapi/oauth/token",
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": jwt},
        )
        r.raise_for_status()
        body = r.json()
        return body["access_token"], int(body.get("expires_in", 3600))


def _list_call_recordings(server: str, token: str, count: int) -> list[dict]:
    date_from = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    with httpx.Client(timeout=30.0) as c:
        r = c.get(
            f"{server}/restapi/v1.0/account/~/call-log",
            headers={"Authorization": f"Bearer {token}"},
            params={"view": "Detailed", "withRecording": "true", "dateFrom": date_from, "perPage": count},
        )
        r.raise_for_status()
        return [rec for rec in r.json().get("records", []) if rec.get("recording")]


def _download_recording(token: str, content_uri: str) -> tuple[bytes, str]:
    with httpx.Client(timeout=60.0, follow_redirects=True) as c:
        r = c.get(content_uri, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "audio/mpeg")


def _with_token_retry(server: str, client_id: str, client_secret: str, jwt: str, fn):
    """Run `fn(token)`, retrying once with a freshly-minted token if the cached one turns
    out to be stale (401) — handles a token that expired mid-cache-life or a JWT that was
    rotated since it was cached."""
    token = _get_token(server, client_id, client_secret, jwt)
    try:
        return fn(token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 401:
            raise
        token = _get_token(server, client_id, client_secret, jwt, force=True)
        return fn(token)


def _to_meta(rec: dict) -> dict:
    """Call-log metadata only — no audio download, no transcription. The listing response
    already includes `recording.contentUri`, so this is effectively free."""
    direction = rec.get("direction")  # "Inbound" | "Outbound"
    frm = rec.get("from") or {}
    to = rec.get("to") or {}
    caller_party, agent_party = (frm, to) if direction == "Inbound" else (to, frm)

    caller = caller_party.get("name") or caller_party.get("phoneNumber") or "Unknown Caller"
    phone = caller_party.get("phoneNumber") or ""
    agent = agent_party.get("name") or agent_party.get("extensionNumber") or agent_party.get("phoneNumber") or "Agent"

    start = rec.get("startTime") or ""
    call_date = start[:10] if len(start) >= 10 else date.today().isoformat()
    secs = int(rec.get("duration") or 0)
    duration = f"{secs // 60}:{secs % 60:02d}"

    recording = rec.get("recording") or {}
    return {
        "ext_id": str(rec.get("id") or recording.get("id") or uuid.uuid4().hex),
        "caller": caller,
        "phone": phone,
        "agent": agent,
        "date": call_date,
        "duration": duration,
        "content_uri": recording.get("contentUri"),
    }


def _list_real(count: int, *, endpoint: str, client_id: str, client_secret: str, jwt: str) -> list[dict]:
    server = _server(endpoint)
    records = _with_token_retry(
        server, client_id, client_secret, jwt,
        lambda token: _list_call_recordings(server, token, count),
    )
    log.info("bt_client: %d recording(s) from BT Cloud Work (live)", len(records))
    return [_to_meta(rec) for rec in records]


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def list_latest(count: int, *, endpoint: str, client_id: str, client_secret: str, jwt: str = "") -> list[dict]:
    """Return up to `count` recordings' metadata (no transcript, no audio download). Live
    when a JWT is configured, otherwise demo data (which already carries a canned
    `transcript`, so callers should skip `transcribe_one` for any item that has one). Live
    failures propagate so the caller can surface them."""
    if jwt:
        return _list_real(count, endpoint=endpoint, client_id=client_id, client_secret=client_secret, jwt=jwt)
    log.info("bt_client: fetching %d demo recording(s) (no JWT configured)", count)
    return _fetch_demo(count)


def transcribe_one(item: dict, *, endpoint: str, client_id: str, client_secret: str, jwt: str) -> str:
    """Download and transcribe a single recording — only called for items `list_latest`
    didn't already provide a transcript for (i.e. never for already-known/demo items)."""
    content_uri = item.get("content_uri")
    if not content_uri:
        return "(no transcript available)"
    server = _server(endpoint)
    try:
        audio, ctype = _with_token_retry(
            server, client_id, client_secret, jwt,
            lambda token: _download_recording(token, content_uri),
        )
        return transcribe(audio, ctype) or "(no transcript available)"
    except Exception as exc:
        log.warning("download/transcribe failed for call %s: %s", item.get("ext_id"), exc)
        return "(no transcript available)"


def fetch_audio(*, endpoint: str, client_id: str, client_secret: str, jwt: str, content_uri: str) -> tuple[bytes, str]:
    """Re-fetch one recording's raw audio bytes on demand for playback — nothing is stored
    locally, mirrors s3_client.download's role for the S3 source (see api.py's
    GET /recordings/{id}/audio). Raises on missing JWT / failed auth / download error; the
    caller is responsible for turning that into an HTTP error."""
    if not jwt:
        raise ValueError("no JWT configured — cannot fetch live audio")
    server = _server(endpoint)
    return _with_token_retry(
        server, client_id, client_secret, jwt,
        lambda token: _download_recording(token, content_uri),
    )


def test_connection(*, endpoint: str, client_id: str, client_secret: str, jwt: str = "") -> str:
    """Validate the connection. With a JWT, performs a real RingCentral auth and returns
    'live'; without one, validates the fields for demo mode and returns 'demo'. Raises on
    bad input / failed auth."""
    if not endpoint:
        raise ValueError("endpoint is required")
    if not jwt:
        return "demo"
    if not client_id or not client_secret:
        raise ValueError("client ID and secret are required for live BT Cloud Work auth")
    _access_token(_server(endpoint), client_id, client_secret, jwt)  # raises on failure
    return "live"
