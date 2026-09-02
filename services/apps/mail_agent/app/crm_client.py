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
    """POST /intake/referral — returns {reference, status, needs_completion, missing_fields}."""
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
