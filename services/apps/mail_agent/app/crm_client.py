"""Client for the external CRM's intake API (pw-crm-be).

Auth is two headers on every call, checked against the CRM's INTAKE_CLIENTS config —
see crm_sync.py for what gets built into each payload and when these are called.
"""

from __future__ import annotations

import httpx

from .config import settings


def _headers() -> dict[str, str]:
    return {"X-Client-Id": settings.crm_client_id, "X-API-Key": settings.crm_api_key}


async def submit_referral(payload: dict) -> dict:
    """POST /intake/referral — returns {reference, status, duplicate, missing_fields, needs_completion}."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{settings.crm_base_url}/intake/referral", json=payload, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def submit_communication(payload: dict) -> dict:
    """POST /intake/communication — a contact against an existing case with nothing to
    action beyond logging it. Returns {..., change_request_id: null} — a reschedule/
    cancel can no longer be raised through this endpoint (see submit_change_request);
    sending request_type/meeting_reference/etc. here, or category CHANGE_OR_CANCEL_SESSION/
    CANCELLATION, now 422s."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{settings.crm_base_url}/intake/communication", json=payload, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def submit_change_request(payload: dict) -> dict:
    """POST /intake/change-request — the only endpoint that can raise a change request
    (a family asking to move or cancel a session). Requires case_ref, meeting_reference,
    and request_type (RESCHEDULE|CANCEL|OTHER) — no fallback if the session can't be
    identified; see crm_sync.py for what happens then. Returns
    {change_request_id, communication_id, case_ref, duplicate, message}."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{settings.crm_base_url}/intake/change-request", json=payload, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def submit_activity(payload: dict) -> dict:
    """POST /intake/activity — returns {activity_ref, duplicate}. Called once per email,
    always, regardless of outcome — a cross-referencing record that the contact was
    handled at all, alongside whichever of submit_referral/submit_communication also ran
    (or didn't). See CRM_ACTIVITY_LOG_PROPOSAL.md for the agreed contract."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{settings.crm_base_url}/intake/activity", json=payload, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def upload_attachment(filename: str, content: bytes, content_type: str) -> dict:
    """POST /intake/attachment — returns {storage_key, file_name, content_type, size_bytes}."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.crm_base_url}/intake/attachment",
            files={"file": (filename, content, content_type)},
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()
