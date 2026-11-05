"""The mail agent's routing table: queue -> may an agent-drafted reply be offered.

What this table does NOT do is decide an email's taxonomy queue. That is settled in
the agent factory, next to the taxonomy that defines it, so the controlled
vocabulary has one home rather than a vendored copy per service that drifts out of
step with the brief. These rules take the route the agent already decided as an
input and answer a different question the taxonomy does not: given that queue, is a
drafted reply something a human may approve at all.

The rules exist as configuration rather than as `if` statements inside the handler
so that the reason a workflow was refused lands in the run's own metadata and can
be read back after the fact.
"""

from __future__ import annotations

import pytest

from app import restriction
from app.orchestration import REPLY_APPROVAL, build_routing_table
from platform_orchestration import RoutingFacts


@pytest.fixture
def table():
    return build_routing_table()


def facts(*, route="referrals", restricted=False, category="new_referral", flags=()):
    return RoutingFacts(
        capability="mail",
        category=category,
        flags=frozenset(flags),
        attributes={"route": route, "restricted": restricted},
    )


# ── the ordinary case ────────────────────────────────────────────────────────

def test_a_replyable_queue_permits_the_reply_workflow(table):
    decision = table.route(facts())
    assert decision.target.workflow == REPLY_APPROVAL
    assert decision.target.metadata["reply_workflow_allowed"] is True


@pytest.mark.parametrize(
    "route", ["info", "referrals", "coordinator", "finance", "reports", "admin"],
)
def test_every_ordinary_queue_is_replyable(table, route):
    decision = table.route(facts(route=route))
    assert decision.target.metadata["reply_workflow_allowed"] is True


# ── restriction overrides ────────────────────────────────────────────────────

def test_a_restricted_email_is_refused_a_reply_workflow(table):
    """Whether to respond to a safeguarding disclosure is a clinical decision."""
    decision = table.route(facts(restricted=True))
    assert decision.target.metadata["reply_workflow_allowed"] is False
    assert decision.queue == restriction.ROUTE_SAFEGUARDING


def test_the_restriction_overrides_an_otherwise_replyable_queue(table):
    """A restricted email sitting in the finance queue is still restricted. The
    override must not be refinable by a queue rule added later."""
    decision = table.route(facts(route="finance", restricted=True))
    assert decision.target.metadata["reply_workflow_allowed"] is False
    assert decision.rule_name == "safeguarding-restricted"


def test_the_safeguarding_route_is_refused_even_without_the_restricted_flag(table):
    """Belt and braces: either signal is enough, matching `restriction.py`, which
    ratchets restriction up and never clears it."""
    decision = table.route(facts(route=restriction.ROUTE_SAFEGUARDING))
    assert decision.target.metadata["reply_workflow_allowed"] is False


def test_the_exception_queue_is_refused_a_reply_workflow(table):
    """Its business entry point was never resolved, so a human handles it."""
    decision = table.route(facts(route=restriction.ROUTE_EXCEPTION))
    assert decision.target.metadata["reply_workflow_allowed"] is False
    assert decision.queue == restriction.ROUTE_EXCEPTION


# ── the decision explains itself ─────────────────────────────────────────────

def test_a_refusal_carries_its_reason(table):
    decision = table.route(facts(restricted=True))
    assert "clinical decision" in decision.reason


def test_the_matching_rule_is_named(table):
    assert table.route(facts(route="exception")).rule_name == "exception-queue"


# ── precedence ───────────────────────────────────────────────────────────────

def test_the_overrides_sit_above_every_queue_rule(table):
    """Negative priorities, per the shared layer's convention, so a queue rule
    added later cannot accidentally take precedence over a restriction."""
    overrides = [r for r in table.rules if r.priority < 0]
    queue_rules = [r for r in table.rules if r.priority >= 0]
    assert overrides, "no restriction overrides in the table"
    assert queue_rules, "no queue rules in the table"
    assert max(r.priority for r in overrides) < min(r.priority for r in queue_rules)


def test_every_override_is_terminal(table):
    """A non-terminal restriction could be refined away by a later rule."""
    for rule in table.rules:
        if rule.priority < 0:
            assert rule.terminal, "override %r is not terminal" % rule.name


def test_an_unknown_queue_still_only_reaches_the_approval_gate(table):
    """A queue nobody anticipated is treated as replyable -- deliberately.

    The catch-all rule has no conditions, so a taxonomy queue added later lands
    here rather than in the fallback. That is safe because "replyable" does not
    mean "sent": it means a draft may be prepared and offered to a human who
    holds the approval grant. The outcomes that must never be offered -- restricted
    material and the exception queue -- are caught by the terminal overrides above,
    which is where the safety actually lives.

    Recorded as a test because the alternative (refuse anything unrecognised)
    would silently stop offering drafts for a queue PW added, and that failure
    looks like the agent having quietly stopped working.
    """
    decision = table.route(facts(route="something_new_in_the_taxonomy"))
    assert decision.matched is True
    assert decision.rule_name == "replyable-queue"
    assert decision.target.metadata["reply_workflow_allowed"] is True
    assert decision.target.workflow == REPLY_APPROVAL


def test_the_table_always_reaches_a_decision(table):
    """A silent no-match is how work goes missing."""
    decision = table.route(RoutingFacts(capability="mail"))
    assert decision.target is not None
    assert "reply_workflow_allowed" in decision.target.metadata
