"""A transient CRM failure must not cost a referral.

The CRM runs on an instance that cold-starts and restarts, so a 502 mid-deploy is
ordinary life rather than a fault, and a rate limiter is coming that answers 429.
Before this retry existed, every one of those became crm_status="failed" — and a
failed row is never re-analysed, because its summary_status is already "done". One
restart on their side would silently lose a referral, which is how the first 19
were lost.

A 422 is the opposite case: a payload they will never accept. Retrying it only
burns attempts, so it must fail on the first response.
"""

from __future__ import annotations

import httpx
import pytest

from app import crm_client


class _FakeClient:
    """Stands in for httpx.AsyncClient, replaying a fixed list of responses."""

    def __init__(self, statuses, calls, headers=None):
        self._statuses = statuses
        self._calls = calls
        self._headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        i = len(self._calls)
        self._calls.append(url)
        status = self._statuses[min(i, len(self._statuses) - 1)]
        return httpx.Response(
            status_code=status,
            json={"reference": "PW-2026-0001"},
            headers=self._headers,
            request=httpx.Request("POST", url),
        )


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """Keep the backoff logic exercised without the wall-clock cost."""
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(crm_client.asyncio, "sleep", _instant)


def _install(monkeypatch, statuses, headers=None):
    calls: list[str] = []
    monkeypatch.setattr(
        crm_client.httpx,
        "AsyncClient",
        lambda **_kw: _FakeClient(statuses, calls, headers),
    )
    return calls


@pytest.mark.asyncio
async def test_recovers_from_a_restart_mid_deploy(monkeypatch):
    calls = _install(monkeypatch, [502, 503, 201])
    out = await crm_client.submit_referral({"idempotency_key": "abc"})
    assert out == {"reference": "PW-2026-0001"}
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_422_is_never_retried(monkeypatch):
    """A payload they will never accept. One attempt, then surface it."""
    calls = _install(monkeypatch, [422])
    with pytest.raises(httpx.HTTPStatusError):
        await crm_client.submit_referral({"idempotency_key": "abc"})
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_429_is_retried(monkeypatch):
    calls = _install(monkeypatch, [429, 201], headers={"Retry-After": "1"})
    out = await crm_client.submit_referral({"idempotency_key": "abc"})
    assert out == {"reference": "PW-2026-0001"}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_gives_up_rather_than_retrying_forever(monkeypatch):
    calls = _install(monkeypatch, [503])
    with pytest.raises(httpx.HTTPStatusError):
        await crm_client.submit_referral({"idempotency_key": "abc"})
    assert len(calls) == crm_client._MAX_RETRIES + 1


@pytest.mark.asyncio
async def test_a_dropped_connection_is_retried(monkeypatch):
    """No verdict came back, so re-sending is safe — the idempotency key is what
    makes that true rather than merely hopeful."""
    calls: list[str] = []

    class _Flaky(_FakeClient):
        async def post(self, url, **kwargs):
            if not calls:
                calls.append(url)
                raise httpx.ConnectError("connection reset")
            return await super().post(url, **kwargs)

    monkeypatch.setattr(
        crm_client.httpx, "AsyncClient", lambda **_kw: _Flaky([201], calls)
    )
    out = await crm_client.submit_referral({"idempotency_key": "abc"})
    assert out == {"reference": "PW-2026-0001"}
    assert len(calls) == 2
