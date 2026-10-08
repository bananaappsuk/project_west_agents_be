"""Resolve Entra group membership into a stored, per-user authorised mailbox set.

The resolved set lives in the database rather than in the access token. A claim
would be faster and equally tamper-proof, but revocation would then lag by the
access-token lifetime -- and for safeguarding material that lag is not acceptable.
A database read is one indexed lookup and is cacheable per request.

Where the group ids come from is deliberately not this module's business. They
arrive from the `groups` claim on an Entra token where it fits, or from
GET /me/memberOf when Entra replaces that claim with an overage indicator. Keeping
resolution independent of the source means the sign-in wiring can change without
touching the authorization logic, and lets the logic be tested without a token.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .mailbox_scope import AccessRule, MailboxScope, resolve
from .models import MailboxAccessRule, SafeguardingClearance, UserMailboxAccess

log = logging.getLogger("mail_agent.access")

#: Entra object id of PW-Safeguarding-Lead. Clearance is a separate axis from mailbox
#: access: safeguarding@ is an alias into coordinator@, which every named Project West
#: user can already read, so a mailbox permission would grant it to everyone.
SAFEGUARDING_GROUP_ID = "a5d90455-e675-458b-ae6d-3c915e22155f"


async def _load_rules(session: AsyncSession) -> list[AccessRule]:
    rows = (await session.execute(select(MailboxAccessRule))).scalars().all()
    return [
        AccessRule(
            entra_group_id=r.entra_group_id,
            mailbox_id=r.mailbox_id,
            can_read=r.can_read,
            can_send=r.can_send,
            can_approve=r.can_approve,
        )
        for r in rows
    ]


async def _has_clearance_record(session: AsyncSession, user_id: str) -> bool:
    row = (await session.execute(
        select(SafeguardingClearance.id).where(
            SafeguardingClearance.user_id == user_id,
            SafeguardingClearance.revoked_at.is_(None),
        ).limit(1)
    )).first()
    return row is not None


async def resolve_and_store(
    session: AsyncSession,
    *,
    user_id: str,
    group_ids: Iterable[str],
) -> MailboxScope:
    """Rebuild this user's authorised set from their current group membership.

    The rows are replaced wholesale rather than merged. A user removed from a group
    must lose that mailbox, and an incremental upsert is exactly the shape of bug
    that leaves a revoked grant behind.
    """
    groups = {g for g in group_ids if g}
    rules = await _load_rules(session)
    cleared = await _has_clearance_record(session, user_id)

    scope = resolve(
        user_id=user_id,
        group_ids=groups,
        rules=rules,
        safeguarding_group_id=SAFEGUARDING_GROUP_ID,
        has_clearance_record=cleared,
    )

    await session.execute(
        delete(UserMailboxAccess).where(UserMailboxAccess.user_id == user_id)
    )
    now = datetime.now(timezone.utc)
    for mailbox_id in scope.readable:
        session.add(UserMailboxAccess(
            user_id=user_id,
            mailbox_id=mailbox_id,
            can_read=True,
            can_send=mailbox_id in scope.sendable,
            can_approve=mailbox_id in scope.approvable,
            resolved_at=now,
        ))

    # The group count is logged alongside the outcome because "resolved to nothing"
    # has two very different causes that look identical from the API: the token
    # carried no groups at all (a configuration problem -- the app registration is
    # not emitting the claim), or it carried groups that match no rule (a legitimate
    # user who has not been granted a mailbox yet). Without this number, diagnosing
    # the first as the second costs an afternoon.
    log.info("AUDIT access resolved user=%s groups_in_token=%d matched_rules=%d "
             "mailboxes=%d send=%d approve=%d safeguarding=%s",
             user_id, len(groups), len({r.mailbox_id for r in rules
                                        if r.entra_group_id in groups}),
             len(scope.readable), len(scope.sendable),
             len(scope.approvable), scope.safeguarding)
    return scope


async def load_scope(session: AsyncSession, user_id: str) -> MailboxScope:
    """This user's stored authorised set.

    Never derived from anything in the request. An empty result is returned as an
    empty scope rather than an error: the API layer decides how to present "you are
    authorised for nothing", and it must say so explicitly rather than rendering an
    empty inbox that reads as "no mail today".
    """
    rows = (await session.execute(
        select(UserMailboxAccess).where(UserMailboxAccess.user_id == user_id)
    )).scalars().all()

    return MailboxScope(
        user_id=user_id,
        readable=frozenset(r.mailbox_id for r in rows if r.can_read),
        sendable=frozenset(r.mailbox_id for r in rows if r.can_send),
        approvable=frozenset(r.mailbox_id for r in rows if r.can_approve),
        safeguarding=await _has_clearance_record(session, user_id),
    )


async def grant_clearance(
    session: AsyncSession, *, user_id: str, granted_by: str | None = None
) -> None:
    """Record safeguarding clearance. Idempotent while an active grant exists."""
    if await _has_clearance_record(session, user_id):
        return
    session.add(SafeguardingClearance(user_id=user_id, granted_by=granted_by))
    log.info("AUDIT safeguarding clearance granted user=%s by=%s", user_id, granted_by)


async def revoke_clearance(session: AsyncSession, *, user_id: str) -> None:
    """Revoke by stamping revoked_at -- the row is kept.

    This grant's history is the one most likely to be audited, so a revocation must
    leave evidence that it was ever held rather than deleting the fact.
    """
    rows = (await session.execute(
        select(SafeguardingClearance).where(
            SafeguardingClearance.user_id == user_id,
            SafeguardingClearance.revoked_at.is_(None),
        )
    )).scalars().all()
    now = datetime.now(timezone.utc)
    for r in rows:
        r.revoked_at = now
    if rows:
        log.info("AUDIT safeguarding clearance revoked user=%s", user_id)
