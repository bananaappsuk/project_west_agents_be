"""The mail agent's authorization adapter for the shared orchestration layer.

Under app-only Graph access the agent reads and sends as the *application*, so
nothing in Microsoft 365 stops it sending as finance@ on behalf of a user with no
Finance rights. This adapter is therefore not defence in depth; it is the defence.
Everything in it fails closed, and these tests are the proof of that for each way
it can be asked.

The three grants are kept apart deliberately -- read, send and approve -- and the
safeguarding axis sits on top of all three, because `safeguarding@` is an alias
into `coordinator@` that every named Project West user can already read.
"""

from __future__ import annotations

import pytest

from app.mailbox_scope import MailboxScope
from app.orchestration import (
    CAPABILITY,
    MailboxAuthorizationPort,
    SystemAuthorizationPort,
)
from platform_orchestration import Principal

ALICE = Principal(user_id="alice")


def scope(
    *,
    readable=("mbx-1",),
    sendable=("mbx-1",),
    approvable=("mbx-1",),
    safeguarding=False,
) -> MailboxScope:
    return MailboxScope(
        user_id="alice",
        readable=frozenset(readable),
        sendable=frozenset(sendable),
        approvable=frozenset(approvable),
        safeguarding=safeguarding,
    )


async def _ask(port, grant: str, subject_ref: str = "email-1") -> bool:
    fn = {
        "read": port.can_view, "send": port.can_start, "approve": port.can_approve,
    }[grant]
    return await fn(ALICE, capability=CAPABILITY, subject_ref=subject_ref)


# ── the ordinary case ────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("grant", ["read", "send", "approve"])
async def test_a_fully_granted_user_is_permitted(patched_sessions, grant):
    port = MailboxAuthorizationPort(scope())
    assert await _ask(port, grant) is True


# ── mailbox scope ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("grant", ["read", "send", "approve"])
async def test_an_out_of_scope_mailbox_is_refused_for_every_grant(patched_sessions, grant):
    """Reading is the floor: no grant survives losing mailbox access."""
    port = MailboxAuthorizationPort(scope(readable=()))
    assert await _ask(port, grant) is False


@pytest.mark.asyncio
async def test_read_access_alone_does_not_permit_starting_a_send(patched_sessions):
    """Starting a reply workflow asserts the reply should go out, so it takes the
    send grant -- not merely the ability to read the mailbox."""
    port = MailboxAuthorizationPort(scope(sendable=(), approvable=()))
    assert await _ask(port, "read") is True
    assert await _ask(port, "send") is False


@pytest.mark.asyncio
async def test_send_access_does_not_confer_approval(patched_sessions):
    """The person who may send is routinely not the person who may authorise."""
    port = MailboxAuthorizationPort(scope(approvable=()))
    assert await _ask(port, "send") is True
    assert await _ask(port, "approve") is False


# ── safeguarding clearance is a separate axis ────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("grant", ["read", "send", "approve"])
async def test_restricted_email_is_refused_without_clearance(
    patched_sessions, store, email_factory, grant
):
    """Every mailbox grant and still invisible: mailbox access must not be a way
    into safeguarding material, because every named user can read coordinator@."""
    store.put(email_factory(restricted=True))
    port = MailboxAuthorizationPort(scope())
    assert await _ask(port, grant) is False


@pytest.mark.asyncio
async def test_restricted_email_is_visible_with_clearance(
    patched_sessions, store, email_factory
):
    store.put(email_factory(restricted=True))
    port = MailboxAuthorizationPort(scope(safeguarding=True))
    assert await _ask(port, "read") is True


@pytest.mark.asyncio
async def test_clearance_alone_does_not_grant_mailbox_access(
    patched_sessions, store, email_factory
):
    """Clearance is an additional axis, not a master key."""
    store.put(email_factory(restricted=True))
    port = MailboxAuthorizationPort(scope(readable=(), safeguarding=True))
    assert await _ask(port, "read") is False


@pytest.mark.asyncio
async def test_approval_of_restricted_material_needs_clearance_too(
    patched_sessions, store, email_factory
):
    """Someone who cannot see an item is in no position to authorise an action
    on it."""
    store.put(email_factory(restricted=True))
    port = MailboxAuthorizationPort(scope(safeguarding=False))
    assert await _ask(port, "approve") is False


# ── failing closed on absence ────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("grant", ["read", "send", "approve"])
async def test_an_unknown_email_is_refused(patched_sessions, grant):
    port = MailboxAuthorizationPort(scope())
    assert await _ask(port, grant, subject_ref="no-such-email") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", ["read", "send", "approve"])
async def test_an_email_with_no_mailbox_is_refused(
    patched_sessions, store, email_factory, grant
):
    """An unscoped row must not be universally readable -- there is nothing to
    check a grant against, and "no mailbox" is not "any mailbox"."""
    store.put(email_factory(mailbox_id=None))
    port = MailboxAuthorizationPort(scope())
    assert await _ask(port, grant) is False


@pytest.mark.asyncio
async def test_an_empty_scope_permits_nothing(patched_sessions):
    port = MailboxAuthorizationPort(
        scope(readable=(), sendable=(), approvable=())
    )
    for grant in ("read", "send", "approve"):
        assert await _ask(port, grant) is False


# ── the scheduler's port ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_system_port_may_run_workflows():
    port = SystemAuthorizationPort()
    system = Principal(user_id="", service=True)
    assert await port.can_start(system, capability=CAPABILITY, subject_ref="e1")
    assert await port.can_view(system, capability=CAPABILITY, subject_ref="e1")


@pytest.mark.asyncio
async def test_the_system_port_never_approves():
    """"The system approved it" is not human review."""
    port = SystemAuthorizationPort()
    system = Principal(user_id="", service=True)
    assert await port.can_approve(
        system, capability=CAPABILITY, subject_ref="e1"
    ) is False


@pytest.mark.asyncio
async def test_the_system_port_refuses_a_non_service_caller():
    """It is the scheduler's port, not a back door for a signed-in user."""
    port = SystemAuthorizationPort()
    assert await port.can_start(ALICE, capability=CAPABILITY, subject_ref="e1") is False


# ── the principal is built from claims only ──────────────────────────────────

def test_the_principal_comes_from_verified_claims():
    from app.orchestration import principal_from_claims
    p = principal_from_claims({
        "sub": "alice", "groups": ["g1", "g2"], "scope": "mail-agent:emails.write",
    })
    assert p.user_id == "alice"
    assert p.groups == frozenset({"g1", "g2"})
    assert "mail-agent:emails.write" in p.scopes
    assert p.is_human


def test_a_missing_group_claim_yields_no_groups_rather_than_failing():
    """Overage is handled upstream in `deps.mailbox_scope`, which refuses the
    request outright; this function must not invent groups."""
    from app.orchestration import principal_from_claims
    assert principal_from_claims({"sub": "alice"}).groups == frozenset()
