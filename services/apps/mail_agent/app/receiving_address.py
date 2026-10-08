"""Deterministic receiving-address resolution.

The mailbox is where the mail *is*; the receiving address is what the sender
*meant*. Three of Project West's aliases collapse onto two physical mailboxes --
`complaints@` and `safeguarding@` both land in `coordinator@` -- so once a message is
sitting in a mailbox nothing in its content reliably says which business entry point
it came through, and those demand very different handling.

That evidence exists only in the headers at the moment of collection. If it is not
captured then, it is gone: it cannot be reconstructed from a stored row later. This
module is therefore deliberately pure -- no I/O, no database, no Graph -- so the one
decision that cannot be redone is also the one that is cheapest to test.

The hard rule: an ambiguous or unresolved message is NEVER silently treated as having
arrived at the polled mailbox. It is marked, and a marked row does not drive
autonomous action. Defaulting quietly would manufacture a business fact out of
missing evidence, which is exactly the failure this module exists to prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

# Status values. Mirrored by the ck_emails_receiving_address_status CHECK constraint;
# keep the two in step.
RESOLVED_SINGLE = "resolved_single"
RESOLVED_PRECEDENCE = "resolved_precedence"
AMBIGUOUS = "ambiguous"
UNRESOLVED = "unresolved"
LEGACY = "legacy"

#: Statuses that represent an actual determination. Anything else is an exception
#: state and must not drive autonomous behaviour.
RESOLVED_STATUSES = frozenset({RESOLVED_SINGLE, RESOLVED_PRECEDENCE})

#: Headers that can carry the delivery target when it is absent from To/Cc -- the
#: Bcc, distribution-list and transport-redirect cases.
#:
#: Verified against the live Project West tenant: Exchange Online does NOT stamp
#: Delivered-To or X-MS-Exchange-Organization-OriginalRecipient on inbound internet
#: mail here, so those alone recover nothing. `Received` IS present, and its SMTP
#: `for <recipient>` clause carries the envelope recipient -- which is precisely the
#: Bcc / distribution-list case that To and Cc cannot answer. The others are kept
#: because they cost nothing and other tenants (or future transport changes) do
#: populate them.
DELIVERY_HEADERS = (
    "x-ms-exchange-organization-originalrecipient",
    "delivered-to",
    "x-original-to",
    "envelope-to",
)

#: `Received` is parsed rather than taken whole: the header is a free-text trace
#: line, and only its `for <addr>` clause names a recipient.
RECEIVED_HEADER = "received"

#: Matches the SMTP `for <someone@example.com>` clause, with or without angle
#: brackets. A message traverses several hops, so every Received line is scanned and
#: all candidates collected -- the intersection against the known-address map is what
#: decides, not the order hops happen to appear in.
_RECEIVED_FOR = re.compile(r"\bfor\s+<?([^;>\s<]+@[^;>\s<]+)", re.IGNORECASE)


@dataclass(frozen=True)
class KnownAddress:
    """One row of the known-address map: a physical mailbox address or an alias."""

    address: str
    mailbox_id: str
    #: False for a physical mailbox's own primary address, True for an alias row.
    is_alias: bool = False
    #: False for tenant routing addresses (*.onmicrosoft.com) -- real delivery
    #: targets that carry no business meaning. They resolve (so mail arriving at one
    #: does not land in the exception path) but imply no queue and no reply identity.
    business: bool = True
    queue: str = ""
    reply_allowed: bool = False
    restricted: bool = False


@dataclass(frozen=True)
class Resolution:
    status: str
    #: None on an exception status. The caller may persist a placeholder to satisfy
    #: NOT NULL, but must persist `status` alongside it -- nothing may read
    #: receiving_address without reading status.
    receiving_address: str | None
    #: The alias the sender used, when the match was an alias. None when the match
    #: was the mailbox's own address.
    original_recipient: str | None
    mailbox_id: str | None
    restricted: bool
    queue: str
    reply_allowed: bool
    #: Every known address that matched, in deterministic order.
    candidates: tuple[str, ...] = ()
    #: The raw recipient evidence the decision was derived from, persisted so a
    #: determination can be audited or re-derived later without the message.
    evidence: Mapping[str, object] = field(default_factory=dict)

    @property
    def is_resolved(self) -> bool:
        return self.status in RESOLVED_STATUSES

    @property
    def allows_autonomous_action(self) -> bool:
        """An exception row never auto-sends, and a restricted one never does either.

        Restriction is checked here as well as at send time because the restricted
        route wins whenever *either* the alias or the classifier fires, and this is
        the earliest point at which the alias half is known.
        """
        return self.is_resolved and not self.restricted


def _norm(addr: str | None) -> str:
    return (addr or "").strip().strip("<>").lower()


def _header_addresses(headers: Iterable[Mapping[str, str]] | None) -> list[str]:
    """Pull delivery targets out of Graph's internetMessageHeaders shape.

    Graph returns `[{"name": ..., "value": ...}, ...]`. A header may legitimately
    repeat (several Delivered-To on a forwarded message), so every occurrence is
    collected rather than the first only.
    """
    if not headers:
        return []
    out: list[str] = []
    for h in headers:
        name = _norm(h.get("name"))
        value = h.get("value") or ""
        if name in DELIVERY_HEADERS:
            # A single header can hold a comma-separated list.
            for part in value.split(","):
                got = _norm(part)
                if got:
                    out.append(got)
        elif name == RECEIVED_HEADER:
            for got in _RECEIVED_FOR.findall(value):
                got = _norm(got)
                if got:
                    out.append(got)
    return out


def _recipient_addresses(recipients: Sequence[Mapping] | None) -> list[str]:
    """Graph `toRecipients`/`ccRecipients` -> flat lowercase address list."""
    if not recipients:
        return []
    out: list[str] = []
    for r in recipients:
        got = _norm(((r or {}).get("emailAddress") or {}).get("address"))
        if got:
            out.append(got)
    return out


def resolve(
    *,
    to_recipients: Sequence[Mapping] | None,
    cc_recipients: Sequence[Mapping] | None,
    headers: Iterable[Mapping[str, str]] | None,
    known: Sequence[KnownAddress],
    precedence: Sequence[str] | None = None,
) -> Resolution:
    """Decide which business address a message arrived at.

    `precedence`, when supplied, is an ordered list of addresses most-specific
    first; the first one present in the candidate set wins. It is deliberately a
    parameter rather than a constant: which of two monitored addresses "wins" when a
    message is sent to both is a Project West business decision, and guessing it in
    code would bury an unmade decision somewhere nobody looks. With no rule supplied
    a multi-way match stays `ambiguous` rather than being resolved arbitrarily.
    """
    by_address = {_norm(k.address): k for k in known}

    to_list = _recipient_addresses(to_recipients)
    cc_list = _recipient_addresses(cc_recipients)
    hdr_list = _header_addresses(headers)

    evidence: dict[str, object] = {
        "to": to_list,
        "cc": cc_list,
        "delivery_headers": hdr_list,
    }

    # Order matters only for determinism of `candidates`; the decision itself is
    # order-independent. To/Cc first, then header evidence, de-duplicated.
    seen: list[str] = []
    for addr in (*to_list, *cc_list, *hdr_list):
        if addr in by_address and addr not in seen:
            seen.append(addr)

    matched = [by_address[a] for a in seen]
    candidates = tuple(seen)
    evidence["candidates"] = list(candidates)

    # Fail closed on restriction: if ANY matched entry point is restricted, the
    # message is restricted -- even when the overall resolution is ambiguous. An
    # ambiguous message that *might* have come through safeguarding@ is treated as
    # though it did. The cost of a false positive is an item a cleared user handles;
    # the cost of a false negative is a safeguarding disclosure in an open queue.
    any_restricted = any(k.restricted for k in matched)

    if not matched:
        # Forward, distribution list, or Bcc. The business meaning is genuinely
        # unknown -- not "the polled mailbox".
        return Resolution(
            status=UNRESOLVED,
            receiving_address=None,
            original_recipient=None,
            mailbox_id=None,
            restricted=any_restricted,
            queue="",
            reply_allowed=False,
            candidates=candidates,
            evidence=evidence,
        )

    if len(matched) == 1:
        return _single(matched[0], RESOLVED_SINGLE, candidates, evidence)

    if precedence:
        order = [_norm(p) for p in precedence]
        for wanted in order:
            for k in matched:
                if _norm(k.address) == wanted:
                    res = _single(k, RESOLVED_PRECEDENCE, candidates, evidence)
                    # A precedence pick must not quietly drop a restriction carried
                    # by one of the addresses it beat.
                    if any_restricted and not res.restricted:
                        return Resolution(**{**res.__dict__, "restricted": True})
                    return res

    # Most-specific-alias-wins, but ONLY within a single mailbox.
    #
    # Observed repeatedly in the live tenant: a message names both an alias and the
    # mailbox it routes into -- `safeguarding@` with `coordinator@`, `complaints@`
    # with `coordinator@`. That is not a genuine conflict between two business
    # functions; it is one destination described twice, and the alias is the more
    # specific description. The SSOT already prefers the most specific alias.
    #
    # The single-mailbox condition is what makes this safe rather than a guess. All
    # candidates resolving to the same mailbox means the authorization outcome is
    # identical whichever is chosen, so this rule can never move a message into a
    # mailbox a reader is not entitled to. Candidates spanning two mailboxes --
    # `coordinator@` with `finance@`, or an all-hands to five of them -- are a real
    # conflict between business functions and stay ambiguous for a human, which is
    # what decision 8 is still open about.
    mailbox_ids = {k.mailbox_id for k in matched}
    if len(mailbox_ids) == 1:
        pick: KnownAddress | None = None
        business_aliases = [k for k in matched if k.is_alias and k.business]
        if len(business_aliases) == 1:
            # One business alias plus its parent: the alias is the specific meaning.
            pick = business_aliases[0]
        elif not business_aliases:
            # No business meaning in contention -- e.g. the mailbox's own address
            # alongside its *.onmicrosoft.com routing address, which is the same
            # destination written two ways. Prefer the mailbox's primary address.
            primaries = [k for k in matched if not k.is_alias]
            if len(primaries) == 1:
                pick = primaries[0]
        # Two business aliases on one mailbox (complaints@ AND safeguarding@) fall
        # through: same destination, genuinely different handling, so a human decides.

        if pick is not None:
            res = _single(pick, RESOLVED_PRECEDENCE, candidates, evidence)
            if any_restricted and not res.restricted:
                return Resolution(**{**res.__dict__, "restricted": True})
            return res

    # Several matched and no rule says which wins. Deliberately NOT resolved.
    return Resolution(
        status=AMBIGUOUS,
        receiving_address=None,
        original_recipient=None,
        mailbox_id=None,
        restricted=any_restricted,
        queue="",
        reply_allowed=False,
        candidates=candidates,
        evidence=evidence,
    )


def _single(
    k: KnownAddress,
    status: str,
    candidates: tuple[str, ...],
    evidence: Mapping[str, object],
) -> Resolution:
    return Resolution(
        status=status,
        receiving_address=k.address,
        # Only an alias carries a distinct business meaning worth recording; a
        # message to a mailbox's own address has no "original recipient" beyond it.
        original_recipient=k.address if k.is_alias else None,
        mailbox_id=k.mailbox_id,
        restricted=k.restricted,
        queue=k.queue,
        reply_allowed=k.reply_allowed,
        candidates=candidates,
        evidence=evidence,
    )
