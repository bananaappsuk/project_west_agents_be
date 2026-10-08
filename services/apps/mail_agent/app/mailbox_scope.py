"""Per-mailbox authorization — the authorised mailbox set for one signed-in user.

Under app-only Graph access the agent reads and sends as the *application*, not as
the person. Exchange's Full Access and Send As checks apply to humans in Outlook; an
app-only token bypasses them entirely. Nothing in Microsoft 365 will stop the
application sending as finance@ on behalf of a user with no Finance rights.

So this module is not defence in depth. It is the defence. Everything here fails
closed, and the scope is never derived from anything in the request.

Two independent axes, because safeguarding@ is an alias into coordinator@ and every
named Project West user holds Full Access on coordinator@ -- if safeguarding were a
mailbox permission it would already be granted to everyone:

  * mailbox access  -- can_read / can_send / can_approve, per mailbox, from Entra groups
  * safeguarding clearance -- a single boolean, from PW-Safeguarding-Lead membership
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


class EmptyScopeError(Exception):
    """The user is authorised for no mailbox at all.

    Raised rather than returning an empty set so the API can say so explicitly.
    Failing closed is correct; failing to an empty list *silently* is not -- the user
    must see an error, not an empty inbox that reads as "no mail today".
    """


class MailboxForbidden(Exception):
    """Carries 404, never 403.

    A 403 confirms the mailbox exists and that someone else can see it. For a system
    holding safeguarding material even that much is a leak, so an out-of-scope read
    is indistinguishable from a row that does not exist.
    """

    status_code = 404
    detail = "not found"


@dataclass(frozen=True)
class AccessRule:
    """One Entra group -> mailbox grant. Rules bind to GROUPS, never to users."""

    entra_group_id: str
    mailbox_id: str
    can_read: bool = False
    can_send: bool = False
    can_approve: bool = False


@dataclass(frozen=True)
class MailboxScope:
    user_id: str
    readable: frozenset[str]
    sendable: frozenset[str]
    approvable: frozenset[str]
    safeguarding: bool = False

    # ── assertions ───────────────────────────────────────────────────────────
    def assert_readable(self, mailbox_id: str) -> None:
        if mailbox_id not in self.readable:
            raise MailboxForbidden()

    def assert_sendable(self, mailbox_id: str) -> None:
        # Deliberately 404 and not "you may read but not send": a caller that cannot
        # send has no business learning the mailbox exists if they cannot read it
        # either. When they CAN read it, assert_readable has already passed and this
        # is the narrower check.
        if mailbox_id not in self.sendable:
            raise MailboxForbidden()

    def assert_approvable(self, mailbox_id: str) -> None:
        if mailbox_id not in self.approvable:
            raise MailboxForbidden()

    # ── query scoping ────────────────────────────────────────────────────────
    def narrow(self, requested: Iterable[str] | None) -> frozenset[str]:
        """The mailbox set a query may touch.

        A mailbox id in a request can only ever NARROW the authorised set. This
        returns an intersection, never the request itself -- which is what stops a
        crafted `?mailbox_id=` widening a user's view. With nothing requested the
        answer is the full authorised set.
        """
        if requested is None:
            return self.readable
        return frozenset(requested) & self.readable

    def visible_restricted(self) -> bool:
        """Whether restricted (safeguarding) rows may appear for this user at all."""
        return self.safeguarding

    @property
    def is_empty(self) -> bool:
        return not self.readable


def resolve(
    *,
    user_id: str,
    group_ids: Iterable[str],
    rules: Sequence[AccessRule],
    safeguarding_group_id: str | None = None,
    has_clearance_record: bool = False,
) -> MailboxScope:
    """Resolve Entra group membership into an authorised mailbox set.

    The UNION is taken where several groups overlap: a user in both
    PW-Mailbox-Info-Read and PW-Mailbox-Info-Send ends up with both rights, rather
    than whichever rule happened to be read last.

    `has_clearance_record` is the safeguarding_clearance table's say-so. Clearance is
    granted by EITHER the Entra group or an explicit clearance record, and it is a
    distinct grant: an application Superadmin does not receive it implicitly. The
    administrative role that configures the system is not the clinical role that
    handles disclosures.
    """
    groups = {g for g in group_ids if g}

    readable: set[str] = set()
    sendable: set[str] = set()
    approvable: set[str] = set()

    for rule in rules:
        if rule.entra_group_id not in groups:
            continue
        if rule.can_read:
            readable.add(rule.mailbox_id)
        if rule.can_send:
            sendable.add(rule.mailbox_id)
        if rule.can_approve:
            approvable.add(rule.mailbox_id)

    # Sending or approving on a mailbox you cannot read is not a coherent state and
    # would let a misconfigured rule leak content through a send/approve path that
    # echoes the message back. Reading is the floor.
    sendable &= readable
    approvable &= readable

    safeguarding = bool(has_clearance_record) or (
        safeguarding_group_id is not None and safeguarding_group_id in groups
    )

    return MailboxScope(
        user_id=user_id,
        readable=frozenset(readable),
        sendable=frozenset(sendable),
        approvable=frozenset(approvable),
        safeguarding=safeguarding,
    )


def groups_from_claims(
    claims: Mapping[str, object],
    *,
    memberof_fallback: Iterable[str] | None = None,
) -> list[str]:
    """Group ids from a verified token, falling back on the overage indicator.

    Entra replaces the `groups` claim with `_claim_names`/`_claim_sources` above
    roughly 200 groups. Rare at Project West's size, but the resolver must fall back
    to GET /me/memberOf rather than resolving to an empty set -- an overage silently
    read as "no groups" would revoke everyone's access at once, and look like an
    empty inbox rather than an error.
    """
    raw = claims.get("groups")
    if isinstance(raw, (list, tuple)) and raw:
        return [str(g) for g in raw if g]

    overage = "_claim_names" in claims or "_claim_sources" in claims
    if overage or not raw:
        if memberof_fallback is None:
            # Caller must perform the memberOf lookup. Refusing here is deliberate:
            # returning [] would be indistinguishable from "member of nothing".
            raise EmptyScopeError(
                "group claim unavailable (overage or absent) and no memberOf fallback supplied"
            )
        return [str(g) for g in memberof_fallback if g]

    return []
