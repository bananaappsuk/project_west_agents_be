"""Context retrieval: only what the caller is allowed to see, with its provenance.

Two requirements meet in this module and both are load-bearing.

*Authorization.* A workflow frequently needs more than the record it was started
over -- the case a referral belongs to, the documents attached to it. Those live in
the CRM and in SharePoint, each with its own access controls, and this layer must
never become a way around either. So a provider is asked for context *on behalf of
a principal*, the engine checks `can_view` before it asks at all, and a provider
that cannot establish entitlement returns nothing rather than returning the record.

*Provenance.* Every fragment of context carries a `SourceReference` back to where
it came from. This is not bookkeeping: the CRM stays the authoritative source for
operational records, so anything the orchestration layer holds is a *copy for the
duration of a run* and has to be traceable to its origin. A summary that cannot be
traced back to the case it was drawn from is also a summary nobody can check.

Nothing here copies a business record into orchestration storage as a new master.
`ContextBundle` is assembled per run, and what persists is the references and
non-sensitive metadata -- never the document body (see `audit.py` for the same rule
applied to events).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .principal import Principal


@dataclass(frozen=True)
class SourceReference:
    """Where a piece of context came from, in enough detail to go back to it.

    `system` is the authoritative owner ("crm", "sharepoint", "mail"), never this
    service. `ref` is that system's own identifier, so the reference stays valid
    independently of any id the orchestration layer assigns.
    """

    system: str
    ref: str
    #: Optional human-facing locator (a case reference, a document name). Safe to
    #: show in a UI; must not carry record content.
    label: str | None = None
    #: Where the caller can go to see it, when the owning system exposes one.
    url: str | None = None

    def key(self) -> str:
        return f"{self.system}:{self.ref}"


@dataclass(frozen=True)
class ContextFragment:
    """One piece of retrieved context and the reference it came from.

    `data` is whatever the provider returns. It is passed to step handlers and is
    deliberately NOT persisted by the engine or written to audit events -- only
    `source` is. That keeps case content and document bodies out of the workflow
    store and out of the logs while leaving a run fully traceable.
    """

    source: SourceReference
    data: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextRequest:
    """What a run needs looked up, on whose behalf."""

    principal: Principal
    capability: str
    subject_ref: str
    #: Named context slots the workflow definition asked for, e.g. ("case",
    #: "documents"). A provider returns what it can and omits what the principal
    #: is not entitled to; the engine does not treat an omission as an error,
    #: because "you may not see this" is a normal answer and the step handler is
    #: the right place to decide whether it can proceed without it.
    slots: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextBundle:
    """The assembled, authorised context for one run."""

    fragments: tuple[ContextFragment, ...] = ()
    #: Slots that were asked for and deliberately withheld, so a handler can tell
    #: "not authorised / not available" apart from "empty". Silence on this point
    #: would let a handler treat a withheld case as a case with no history.
    withheld: tuple[str, ...] = ()

    def get(self, system: str) -> tuple[ContextFragment, ...]:
        return tuple(f for f in self.fragments if f.source.system == system)

    def references(self) -> tuple[SourceReference, ...]:
        """Just the provenance -- what the engine persists and audits."""
        return tuple(f.source for f in self.fragments)

    def was_withheld(self, slot: str) -> bool:
        return slot in self.withheld


@runtime_checkable
class ContextProvider(Protocol):
    """Supplied by the host service. The engine never reaches a source directly."""

    async def fetch(self, request: ContextRequest) -> ContextBundle:
        ...


class NullContextProvider:
    """Returns nothing, and reports every requested slot as withheld.

    The default. A workflow whose steps need no external context runs perfectly
    well against it, and one that does need context gets an explicit "withheld"
    rather than an empty bundle that reads as "there is nothing there".
    """

    async def fetch(self, request: ContextRequest) -> ContextBundle:
        return ContextBundle(withheld=tuple(request.slots))


class StaticContextProvider:
    """Serves a fixed bundle. For tests and for steps whose context is computed
    by the host before the run starts rather than looked up during it."""

    def __init__(self, fragments: Sequence[ContextFragment] = ()) -> None:
        self._fragments = tuple(fragments)

    async def fetch(self, request: ContextRequest) -> ContextBundle:
        return ContextBundle(fragments=self._fragments)
