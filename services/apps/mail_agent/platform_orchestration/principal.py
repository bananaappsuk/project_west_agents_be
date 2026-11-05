"""Who is asking, and what the orchestration layer is allowed to let them do.

The engine deliberately knows nothing about Entra groups, mailboxes, CRM record
ownership or SharePoint document libraries. It knows only that before it starts a
workflow, retrieves context for one, or acts on an approval, it must ask an
`AuthorizationPort` supplied by the host service -- and that a port which says no,
or a port that is absent, means no.

This is the seam that keeps the orchestration core free of capability-specific
business logic while still enforcing authorization *on the backend*. The mail agent
implements the port over its existing `MailboxScope`; a future reports capability
implements it over SharePoint's own access controls. Neither one's rules leak in
here, and neither one can be skipped, because every entry point on the engine takes
a `Principal` and consults the port.

What a `Principal` is NOT: anything read from a request body or query string. The
host builds it from verified token claims only. The engine cannot enforce that
(it cannot see the request), which is precisely why it is stated here and why the
mail agent's adapter derives the principal from `auth.claims` alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Principal:
    """The caller, as the orchestration layer sees them.

    Frozen: a principal that can be mutated after an authorization check has
    passed is a principal that can be escalated between the check and the action.
    """

    #: Stable subject id from the verified token (`sub`). "" is never a valid
    #: authenticated user and ports should treat it as such.
    user_id: str
    #: Opaque to the engine; a port interprets them. Carried so a port does not
    #: have to re-fetch what the host already resolved.
    groups: frozenset[str] = frozenset()
    #: Scopes/permissions the token carried, for ports that key off them.
    scopes: frozenset[str] = frozenset()
    #: True when this is a service-to-service caller (the internal-key path the
    #: agent factory already uses for the mail agent's cron), not a person.
    #: Kept explicit because an automated caller must still be refused the
    #: approval path -- "the system approved it" is not human review.
    service: bool = False
    #: Free-form, host-supplied. Never consulted by the engine.
    attributes: dict[str, object] = field(default_factory=dict)

    @property
    def is_human(self) -> bool:
        return not self.service and bool(self.user_id)


#: The principal a background job runs as. Has no user id and no groups, so any
#: port that gates on identity refuses it by default -- a scheduler that starts
#: workflows must be granted that explicitly by the host's port, not by accident.
SYSTEM_PRINCIPAL = Principal(user_id="", service=True)


@runtime_checkable
class AuthorizationPort(Protocol):
    """What the host must answer before the engine will act.

    The methods are async because a real adapter needs I/O to answer: deciding
    whether a principal may act on an email means resolving which mailbox that
    email belongs to, and the mail agent's own access rules live in its database.
    A synchronous port would force every host to pre-resolve its entire authorised
    set before calling the engine, which is both wasteful and the kind of
    requirement that gets quietly skipped.

    Every method fails closed by contract: a port that cannot determine an answer
    returns False rather than raising, and the engine treats a False as a refusal
    it does not retry. `DenyAll` below is the default wherever a host supplies no
    port, so forgetting to wire one up disables the engine rather than opening it.
    """

    async def can_start(
        self, principal: Principal, *, capability: str, subject_ref: str
    ) -> bool:
        """May this principal start a workflow of this capability over this record?"""

    async def can_view(
        self, principal: Principal, *, capability: str, subject_ref: str
    ) -> bool:
        """May this principal know this workflow exists, and read its context?"""

    async def can_approve(
        self, principal: Principal, *, capability: str, subject_ref: str
    ) -> bool:
        """May this principal make the human approval decision on this workflow?

        Separate from `can_start` and from `can_view` on purpose. In the mail
        agent these are three different grants (`can_read`, `can_send`,
        `can_approve`) and a user who may read a mailbox is routinely not the
        person who may authorise an action in it.
        """


class DenyAll:
    """The default port: refuses everything.

    Chosen as the default so that a host which neglects to pass a port gets a
    service that does nothing, rather than a service that authorises everything.
    The failure is loud, immediate and safe.
    """

    async def can_start(
        self, principal: Principal, *, capability: str, subject_ref: str
    ) -> bool:
        return False

    async def can_view(
        self, principal: Principal, *, capability: str, subject_ref: str
    ) -> bool:
        return False

    async def can_approve(
        self, principal: Principal, *, capability: str, subject_ref: str
    ) -> bool:
        return False


class AllowAll:
    """Permits everything. For library tests and local development only.

    Named so that its presence in a service's startup wiring is obvious in review
    and in a grep. It is never the default.
    """

    async def can_start(
        self, principal: Principal, *, capability: str, subject_ref: str
    ) -> bool:
        return True

    async def can_view(
        self, principal: Principal, *, capability: str, subject_ref: str
    ) -> bool:
        return True

    async def can_approve(
        self, principal: Principal, *, capability: str, subject_ref: str
    ) -> bool:
        return True
