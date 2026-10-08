"""Reply identity — derived, never chosen.

The From address on an outgoing reply is computed from the mailbox the message
arrived at, the alias it arrived through, and the rights of the person approving it.
No caller supplies a sender: the frontend sends no sender field, and a `from` in a
request body is ignored rather than honoured.

Why this matters more than it looks: under app-only Graph the application can send as
any mailbox inside its Exchange RBAC scope regardless of which user triggered it.
Exchange will not refuse a send on behalf of a user with no Finance rights. So the
can_send check here is not a convenience mirroring Exchange — it is the only control,
with nothing underneath it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .mailbox_scope import MailboxScope
from .receiving_address import RESOLVED_STATUSES

# Refusal reasons. Stable strings: they are written to the audit trail and read by
# the UI to explain why a draft is sitting unsent.
NO_SEND_RIGHT = "no_send_right"
ALIAS_REPLY_FORBIDDEN = "alias_reply_forbidden"
RESTRICTED_NO_AUTONOMOUS = "restricted_no_autonomous_reply"
UNRESOLVED_RECEIVING_ADDRESS = "unresolved_receiving_address"
NO_MAILBOX = "no_mailbox"


@dataclass(frozen=True)
class SendIdentity:
    """An authorised From identity plus the decision trail behind it."""

    from_address: str
    mailbox_id: str
    #: "mailbox" when replying as the mailbox's own address, or "alias:<address>".
    #: Recorded on the audit row so the alias decision is reconstructable later.
    alias_decision: str
    approved_by: str | None


@dataclass(frozen=True)
class SendRefusal:
    reason: str
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return False


@dataclass(frozen=True)
class OutboundContext:
    """Everything needed to decide a reply identity, with no database access."""

    mailbox_id: str | None
    mailbox_address: str | None
    receiving_address: str | None
    receiving_address_status: str
    #: The alias row matched at ingestion, if the message arrived through one.
    alias_address: str | None = None
    alias_reply_allowed: bool = False
    #: True when the alias is restricted, the route is safeguarding_restricted, or
    #: the classifier raised safeguarding_indicator.
    restricted: bool = False


def derive(
    ctx: OutboundContext,
    *,
    scope: MailboxScope,
    autonomous: bool,
    actor_user_id: str | None = None,
) -> SendIdentity | SendRefusal:
    """Decide the From identity for a reply, or refuse with a reason.

    `autonomous` distinguishes an unattended auto-send from a human pressing Reply.
    A human with the right to send may reply to a restricted item; the agent may
    never do so on its own.
    """
    if not ctx.mailbox_id or not ctx.mailbox_address:
        return SendRefusal(NO_MAILBOX, "the email carries no mailbox identity")

    # An exception-state receiving address is not a business fact. Replying from a
    # guess would put the wrong organisational identity on outbound mail, which is
    # worse than not replying.
    if ctx.receiving_address_status not in RESOLVED_STATUSES:
        return SendRefusal(
            UNRESOLVED_RECEIVING_ADDRESS,
            f"receiving address is {ctx.receiving_address_status}",
        )

    # Restricted material: the agent never sends a substantive reply by itself,
    # whichever address the message arrived at. An info@ message the classifier
    # flagged safeguarding_indicator is as restricted as one sent to safeguarding@ —
    # the restricted route wins whenever either fires.
    if ctx.restricted and autonomous:
        return SendRefusal(
            RESTRICTED_NO_AUTONOMOUS,
            "restricted items require a cleared human to approve any reply",
        )

    # The only control. Checked before any alias reasoning so that a user without
    # send rights learns nothing about the alias configuration.
    if ctx.mailbox_id not in scope.sendable:
        return SendRefusal(NO_SEND_RIGHT, "user has no can_send on this mailbox")

    arrived_via_alias = bool(ctx.alias_address) and (
        (ctx.receiving_address or "").lower() == (ctx.alias_address or "").lower()
    )

    if arrived_via_alias:
        if not ctx.alias_reply_allowed:
            # safeguarding@ is configured reply_allowed=false: no user and no agent
            # sends from it until PW approves safeguarding leads. Data configuration,
            # not a code branch — which is why this reads the flag rather than the
            # address.
            return SendRefusal(
                ALIAS_REPLY_FORBIDDEN,
                f"replies as {ctx.alias_address} are not permitted",
            )
        return SendIdentity(
            from_address=ctx.alias_address,  # type: ignore[arg-type]
            mailbox_id=ctx.mailbox_id,
            alias_decision=f"alias:{ctx.alias_address}",
            approved_by=actor_user_id,
        )

    return SendIdentity(
        from_address=ctx.mailbox_address,
        mailbox_id=ctx.mailbox_id,
        alias_decision="mailbox",
        approved_by=actor_user_id,
    )
