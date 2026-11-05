"""Configurable routing: which workflow handles a piece of work, and where it lands.

Routing here is a table of declarative rules evaluated against a set of facts. The
facts are supplied by the caller (category, flags, capability, plus whatever else a
capability wants to match on); the table is configuration. Neither the rules nor
this module contain business logic about what a "safeguarding concern" means or
which mailbox "finance" is -- those belong to the capability and to the existing
Project West taxonomy respectively, and duplicating them here is precisely the
drift the taxonomy module already warns against.

So this module provides the *mechanism*: match facts, pick a target, with explicit
precedence and a guaranteed fallback. The mail agent supplies its rules from the
taxonomy it already owns, and the engine never learns what any of it means.

Three properties the design turns on:

  * **Priority is explicit, not positional.** Rules carry a number and are sorted
    by it. A table assembled from several sources (defaults plus per-tenant
    overrides) then has a defined outcome regardless of the order the parts were
    concatenated in, which an "first match in list order" scheme does not.

  * **There is always an answer.** An unmatched fact set routes to the table's
    fallback rather than returning None. A silent no-match is how work goes
    missing; the Project West taxonomy already models this with its `exception`
    queue, and routing follows the same instinct.

  * **An override wins over a match.** Rules marked `terminal` stop evaluation.
    This is what lets a restriction rule ("anything flagged safeguarding goes to
    the restricted queue, whatever else it is") sit above the category rules
    without having to be duplicated into each of them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RoutingFacts:
    """What a routing decision is made from.

    `attributes` keeps the mechanism open: a capability matches on whatever it
    has, without this module growing a field per capability. Scheduling can match
    on `{"lead_time_hours": 2}` and Invoicing on `{"amount_band": "over_10k"}`
    with no change here.
    """

    capability: str
    category: str = ""
    flags: frozenset[str] = frozenset()
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def get(self, key: str) -> Any:
        """Facts as a flat lookup, so a condition does not care where a fact lives."""
        if key == "capability":
            return self.capability
        if key == "category":
            return self.category
        if key == "flags":
            return self.flags
        return self.attributes.get(key)


@dataclass(frozen=True)
class RoutingTarget:
    """Where matched work goes.

    `queue` is a logical destination name, never a mailbox address or a person --
    the same separation the taxonomy already insists on, so that re-pointing a
    queue is configuration rather than reclassification.
    """

    queue: str
    #: The workflow definition to run. Empty means "the caller already knows which
    #: workflow it is running and only wants the queue", which is the mail agent's
    #: case today: one ingest workflow, many queues.
    workflow: str = ""
    #: Which capability service owns the work from here. Usually the capability
    #: that raised it; set differently to hand work across a service boundary (a
    #: report request arriving by email routing to the reports capability).
    capability: str = ""
    #: Carried through to the run's metadata. Non-sensitive routing annotations.
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoutingRule:
    """One rule: if every condition holds, route here.

    Conditions are ANDed. A rule needing OR is expressed as two rules at the same
    priority, which keeps each rule independently readable -- and readable matters
    more than compact for a table an operator may have to audit after an incident.

    Condition values are matched as:
      * a set/frozenset/list/tuple -- the fact must be a member, OR, where the
        fact is itself a collection (flags), the two must intersect;
      * anything else -- equality.
    """

    target: RoutingTarget
    conditions: Mapping[str, Any] = field(default_factory=dict)
    #: Lower runs first. Restriction overrides sit at negative priorities by
    #: convention, so they precede any category rule added later without anyone
    #: having to renumber the table.
    priority: int = 100
    #: Stop evaluating on a match. For overrides that must not be refined further.
    terminal: bool = False
    #: Why this rule exists. Surfaced in the decision, so "why did this go here?"
    #: is answerable from the routing result alone.
    reason: str = ""
    name: str = ""

    def matches(self, facts: RoutingFacts) -> bool:
        for key, expected in self.conditions.items():
            actual = facts.get(key)
            if isinstance(expected, (set, frozenset, list, tuple)):
                expected_set = set(expected)
                if isinstance(actual, (set, frozenset, list, tuple)):
                    # Collection vs collection: intersection. This is what makes
                    # {"flags": {"urgent", "court_deadline"}} mean "carries any of
                    # these", which is how cross-cutting flags are actually used.
                    if not expected_set & set(actual):
                        return False
                elif actual not in expected_set:
                    return False
            else:
                if isinstance(actual, (set, frozenset, list, tuple)):
                    if expected not in set(actual):
                        return False
                elif actual != expected:
                    return False
        return True


@dataclass(frozen=True)
class RoutingDecision:
    """The outcome, with the reasoning attached.

    The engine persists this on the run and audits it. Recording *which rule* fired
    rather than only where the work went is what makes a routing table auditable:
    after a re-tune, a past decision still explains itself.
    """

    target: RoutingTarget
    rule_name: str
    reason: str
    matched: bool

    @property
    def queue(self) -> str:
        return self.target.queue


class RoutingTable:
    """An ordered rule set with a guaranteed fallback."""

    def __init__(
        self,
        rules: Iterable[RoutingRule] = (),
        *,
        fallback: RoutingTarget | None = None,
    ) -> None:
        # Sorted once at construction. Ties keep insertion order (Python's sort is
        # stable), so a table built from defaults-then-overrides behaves the way
        # whoever concatenated it would expect.
        self._rules: list[RoutingRule] = sorted(rules, key=lambda r: r.priority)
        #: Where unmatched work goes. "exception" by default, matching the
        #: taxonomy's own name for "a human needs to look at this", because an
        #: unroutable item is exactly that.
        self._fallback = fallback or RoutingTarget(queue="exception")

    def add(self, rule: RoutingRule) -> RoutingTable:
        """Add a rule and keep the table sorted. Returns self for chaining."""
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority)
        return self

    def extend(self, rules: Iterable[RoutingRule]) -> RoutingTable:
        for rule in rules:
            self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority)
        return self

    @property
    def rules(self) -> Sequence[RoutingRule]:
        return tuple(self._rules)

    def route(self, facts: RoutingFacts) -> RoutingDecision:
        """The destination for these facts. Never returns None.

        A terminal match returns immediately. A non-terminal match is remembered
        and evaluation continues, so a later, more specific rule at a higher
        priority number can refine it -- but a *missing* later rule still leaves
        the earlier match in place rather than falling through to the fallback.
        """
        chosen: RoutingRule | None = None
        for rule in self._rules:
            if not rule.matches(facts):
                continue
            if rule.terminal:
                return RoutingDecision(
                    target=rule.target,
                    rule_name=rule.name or "<unnamed>",
                    reason=rule.reason or "terminal rule matched",
                    matched=True,
                )
            chosen = rule

        if chosen is None:
            return RoutingDecision(
                target=self._fallback,
                rule_name="<fallback>",
                reason="no rule matched; routed to the fallback queue for review",
                matched=False,
            )

        return RoutingDecision(
            target=chosen.target,
            rule_name=chosen.name or "<unnamed>",
            reason=chosen.reason or "rule matched",
            matched=True,
        )
