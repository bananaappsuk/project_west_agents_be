"""Configurable routing: the mechanism, not any capability's rules.

These tests use deliberately Project-West-shaped facts (categories and
cross-cutting flags) because that is the real caller, but nothing here encodes
what a category *means* -- the rules are supplied as data, exactly as a host
supplies them from the taxonomy it already owns.

The properties that matter: priority decides precedence rather than list order,
there is always an answer, a terminal override wins outright, and the decision
explains itself afterwards.
"""

from __future__ import annotations

import pytest

from platform_orchestration import (
    RoutingFacts,
    RoutingRule,
    RoutingTable,
    RoutingTarget,
)


def facts(**kw):
    kw.setdefault("capability", "mail")
    if "flags" in kw:
        kw["flags"] = frozenset(kw["flags"])
    return RoutingFacts(**kw)


@pytest.fixture
def table():
    """A table shaped like the mail agent's: an override, then category rules."""
    return RoutingTable(
        rules=[
            RoutingRule(
                name="safeguarding-override",
                target=RoutingTarget(queue="safeguarding_restricted"),
                conditions={"flags": {"safeguarding_indicator"}},
                priority=-100,
                terminal=True,
                reason="a safeguarding indicator overrides the category's own route",
            ),
            RoutingRule(
                name="referrals",
                target=RoutingTarget(queue="referrals"),
                conditions={"category": {"new_referral", "referral_update"}},
            ),
            RoutingRule(
                name="finance",
                target=RoutingTarget(queue="finance"),
                conditions={"category": "payment_or_finance"},
            ),
        ],
        fallback=RoutingTarget(queue="exception"),
    )


# ── ordinary matching ────────────────────────────────────────────────────────

def test_a_category_routes_to_its_queue(table):
    assert table.route(facts(category="payment_or_finance")).queue == "finance"


def test_a_rule_can_match_several_categories(table):
    assert table.route(facts(category="new_referral")).queue == "referrals"
    assert table.route(facts(category="referral_update")).queue == "referrals"


def test_the_matching_rule_is_named_in_the_decision(table):
    """"Why did this go here?" has to be answerable from the decision alone,
    including after the table has been re-tuned."""
    decision = table.route(facts(category="new_referral"))
    assert decision.rule_name == "referrals"
    assert decision.matched is True


# ── there is always an answer ────────────────────────────────────────────────

def test_an_unmatched_fact_set_routes_to_the_fallback(table):
    """A silent no-match is how work goes missing."""
    decision = table.route(facts(category="something_new"))
    assert decision.queue == "exception"
    assert decision.matched is False
    assert "no rule matched" in decision.reason


def test_an_empty_table_still_answers():
    decision = RoutingTable().route(facts(category="anything"))
    assert decision.queue == "exception"
    assert decision.matched is False


# ── overrides ────────────────────────────────────────────────────────────────

def test_a_terminal_override_beats_the_category(table):
    """The cross-cutting flag rule: restricted wins whatever else the item is."""
    decision = table.route(
        facts(category="payment_or_finance", flags={"safeguarding_indicator"})
    )
    assert decision.queue == "safeguarding_restricted"
    assert decision.rule_name == "safeguarding-override"


def test_the_override_applies_to_an_otherwise_unmatched_item(table):
    decision = table.route(facts(category="", flags={"safeguarding_indicator"}))
    assert decision.queue == "safeguarding_restricted"


def test_an_unrelated_flag_does_not_trigger_the_override(table):
    decision = table.route(facts(category="payment_or_finance", flags={"urgent"}))
    assert decision.queue == "finance"


# ── precedence is by priority, not insertion order ───────────────────────────

def test_priority_decides_precedence_regardless_of_insertion_order():
    """A table assembled from defaults plus overrides has a defined outcome
    however the parts were concatenated."""
    specific = RoutingRule(
        name="specific", target=RoutingTarget(queue="specific"),
        conditions={"category": "x"}, priority=10, terminal=True,
    )
    general = RoutingRule(
        name="general", target=RoutingTarget(queue="general"),
        conditions={"category": "x"}, priority=5, terminal=True,
    )
    forwards = RoutingTable(rules=[specific, general])
    backwards = RoutingTable(rules=[general, specific])
    assert forwards.route(facts(category="x")).rule_name == "general"
    assert backwards.route(facts(category="x")).rule_name == "general"


def test_a_later_non_terminal_rule_refines_an_earlier_match():
    table = RoutingTable(rules=[
        RoutingRule(
            name="broad", target=RoutingTarget(queue="coordinator"),
            conditions={"capability": "mail"}, priority=10,
        ),
        RoutingRule(
            name="narrow", target=RoutingTarget(queue="management"),
            conditions={"capability": "mail", "category": "complaint"}, priority=20,
        ),
    ])
    assert table.route(facts(category="complaint")).queue == "management"
    assert table.route(facts(category="general_enquiry")).queue == "coordinator"


def test_a_missing_refinement_leaves_the_earlier_match_in_place():
    """A broad match is not lost just because no narrower rule exists."""
    table = RoutingTable(rules=[
        RoutingRule(
            name="broad", target=RoutingTarget(queue="coordinator"),
            conditions={"capability": "mail"}, priority=10,
        ),
    ])
    assert table.route(facts(category="anything")).queue == "coordinator"


def test_add_keeps_the_table_sorted():
    table = RoutingTable(rules=[
        RoutingRule(
            name="late", target=RoutingTarget(queue="late"),
            conditions={"category": "x"}, priority=50, terminal=True,
        ),
    ])
    table.add(RoutingRule(
        name="early", target=RoutingTarget(queue="early"),
        conditions={"category": "x"}, priority=1, terminal=True,
    ))
    assert table.route(facts(category="x")).rule_name == "early"


# ── condition semantics ──────────────────────────────────────────────────────

def test_a_set_condition_against_a_collection_fact_is_an_intersection():
    """"Carries any of these flags" -- how cross-cutting flags are actually used."""
    table = RoutingTable(rules=[
        RoutingRule(
            name="urgent", target=RoutingTarget(queue="urgent"),
            conditions={"flags": {"urgent", "court_deadline"}}, terminal=True,
        ),
    ])
    assert table.route(facts(flags={"court_deadline"})).queue == "urgent"
    assert table.route(facts(flags={"sensitive"})).queue == "exception"


def test_a_scalar_condition_against_a_collection_fact_tests_membership():
    table = RoutingTable(rules=[
        RoutingRule(
            name="sensitive", target=RoutingTarget(queue="restricted"),
            conditions={"flags": "sensitive"}, terminal=True,
        ),
    ])
    assert table.route(facts(flags={"sensitive", "urgent"})).queue == "restricted"
    assert table.route(facts(flags={"urgent"})).queue == "exception"


def test_conditions_are_anded():
    table = RoutingTable(rules=[
        RoutingRule(
            name="both", target=RoutingTarget(queue="both"),
            conditions={"category": "complaint", "flags": "urgent"}, terminal=True,
        ),
    ])
    assert table.route(facts(category="complaint", flags={"urgent"})).queue == "both"
    assert table.route(facts(category="complaint")).queue == "exception"


def test_an_arbitrary_attribute_can_be_matched_on():
    """The mechanism stays open: Scheduling and Invoicing match on their own facts
    without this module growing a field per capability."""
    table = RoutingTable(rules=[
        RoutingRule(
            name="big-invoice", target=RoutingTarget(queue="management"),
            conditions={"amount_band": "over_10k"}, terminal=True,
        ),
    ])
    decision = table.route(RoutingFacts(
        capability="invoicing", attributes={"amount_band": "over_10k"},
    ))
    assert decision.queue == "management"


def test_capability_can_be_routed_on_so_work_crosses_a_service_boundary():
    table = RoutingTable(rules=[
        RoutingRule(
            name="report-request",
            target=RoutingTarget(
                queue="reports", capability="reports", workflow="reports.prepare",
            ),
            conditions={"capability": "mail", "category": "report_or_document"},
            terminal=True,
        ),
    ])
    decision = table.route(facts(category="report_or_document"))
    assert decision.target.capability == "reports"
    assert decision.target.workflow == "reports.prepare"


def test_a_rule_with_no_conditions_matches_everything():
    """A legitimate way to express a default at a chosen priority."""
    table = RoutingTable(rules=[
        RoutingRule(name="catch-all", target=RoutingTarget(queue="info")),
    ])
    assert table.route(facts(category="whatever")).queue == "info"
