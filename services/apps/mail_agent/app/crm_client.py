"""Client for the external CRM's intake API (pw-crm-be).

Auth is two headers on every call, checked against the CRM's INTAKE_CLIENTS config —
see crm_sync.py for what gets built into each payload and when these are called.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from .config import settings

log = logging.getLogger("mail_agent.crm_client")

#: Statuses worth trying again. 429 is the CRM's rate limiter; the 5xx values are
#: ordinary life on a Render starter instance, which cold-starts and restarts, so a
#: 502 mid-deploy is an event rather than a fault.
#:
#: 422 is deliberately absent, and that is the important part: it means a payload
#: the CRM will never accept, so retrying only burns attempts. Those are logged for
#: a person instead.
#:
#: Without this, every one of these became crm_status="failed" permanently — and a
#: failed row is never re-analysed, because its summary_status is already "done".
#: One restart on their side would silently lose a referral, which is exactly how
#: the first 19 were lost.
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_TIMEOUT = 30.0


def _headers() -> dict[str, str]:
    return {"X-Client-Id": settings.crm_client_id, "X-API-Key": settings.crm_api_key}


async def _post(path: str, *, json: dict | None = None, files: dict | None = None) -> dict:
    """POST to the CRM, retrying transient failures and honouring Retry-After.

    Raises on a non-retryable error, which the caller already treats as this one
    row's problem and never the run's — see crm_sync's module docstring.
    """
    url = f"{settings.crm_base_url}{path}"
    delay = 2.0
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(url, json=json, files=files, headers=_headers())
            if resp.status_code not in _RETRY_STATUS:
                resp.raise_for_status()
                return resp.json()
            wait = delay
            # Their Retry-After is authoritative when present. Backing off faster
            # than a limiter asks is how a throttle becomes a longer throttle.
            hdr = resp.headers.get("Retry-After")
            if hdr:
                try:
                    wait = max(wait, float(hdr))
                except ValueError:
                    pass
            last_exc = httpx.HTTPStatusError(
                f"{resp.status_code} from {path}", request=resp.request, response=resp
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            # A connection reset or timeout is the same class of problem as a 503:
            # the request never got a verdict, so it is safe to send again. The
            # idempotency key is what makes that safe rather than hopeful.
            wait, last_exc = delay, exc

        if attempt == _MAX_RETRIES:
            break
        log.info("CRM %s transient failure, retrying in %.0fs (attempt %d/%d): %s",
                 path, wait, attempt + 1, _MAX_RETRIES, last_exc)
        await asyncio.sleep(min(wait, 60))
        delay *= 2

    raise last_exc  # type: ignore[misc]


async def submit_referral(payload: dict) -> dict:
    """POST /intake/referral — returns {reference, status, duplicate, missing_fields, needs_completion}."""
    return await _post("/intake/referral", json=payload)


async def submit_communication(payload: dict) -> dict:
    """POST /intake/communication — a contact against an existing case with nothing to
    action beyond logging it. Returns {..., change_request_id: null} — a reschedule/
    cancel can no longer be raised through this endpoint (see submit_change_request);
    sending request_type/meeting_reference/etc. here, or category CHANGE_OR_CANCEL_SESSION/
    CANCELLATION, now 422s."""
    return await _post("/intake/communication", json=payload)


async def submit_change_request(payload: dict) -> dict:
    """POST /intake/change-request — the only endpoint that can raise a change request
    (a family asking to move or cancel a session). Requires case_ref, meeting_reference,
    and request_type (RESCHEDULE|CANCEL|OTHER) — no fallback if the session can't be
    identified; see crm_sync.py for what happens then. Returns
    {change_request_id, communication_id, case_ref, duplicate, message}."""
    return await _post("/intake/change-request", json=payload)


async def submit_activity(payload: dict) -> dict:
    """POST /intake/activity — returns {activity_ref, duplicate}. Called once per email,
    always, regardless of outcome — a cross-referencing record that the contact was
    handled at all, alongside whichever of submit_referral/submit_communication also ran
    (or didn't). See CRM_ACTIVITY_LOG_PROPOSAL.md for the agreed contract."""
    return await _post("/intake/activity", json=payload)


async def upload_attachment(filename: str, content: bytes, content_type: str) -> dict:
    """POST /intake/attachment — returns {storage_key, file_name, content_type, size_bytes}."""
    return await _post("/intake/attachment", files={"file": (filename, content, content_type)})
