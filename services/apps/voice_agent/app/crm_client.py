"""Client for the external CRM's intake API (pw-crm-be).

Auth is two headers on every call, checked against the CRM's INTAKE_CLIENTS config —
see crm_sync.py for what gets built into each payload and when these are called. No
attachment upload here — call recordings have no attachment path in the CRM's intake
contract (duration_seconds is sent instead, see crm_sync.py).
"""

from __future__ import annotations

import httpx

from .config import settings


def _headers() -> dict[str, str]:
    return {"X-Client-Id": settings.crm_client_id, "X-API-Key": settings.crm_api_key}


async def submit_referral(payload: dict) -> dict:
    """POST /intake/referral — returns {submission_ref, status, duplicate, missing_fields, needs_completion}."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{settings.crm_base_url}/intake/referral", json=payload, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def submit_communication(payload: dict) -> dict:
    """POST /intake/communication — returns {..., change_request_id} when a reschedule/cancel was raised."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{settings.crm_base_url}/intake/communication", json=payload, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def submit_activity(payload: dict) -> dict:
    """POST /intake/activity — returns {activity_ref, duplicate}. Called once per recording,
    always, regardless of outcome — a cross-referencing record that the contact was handled
    at all, alongside whichever of submit_referral/submit_communication also ran (or didn't).
    See CRM_ACTIVITY_LOG_PROPOSAL.md for the agreed contract."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{settings.crm_base_url}/intake/activity", json=payload, headers=_headers())
        resp.raise_for_status()
        return resp.json()
