"""Merging classification with what ingestion established.

The rule under test: the restricted route wins whenever EITHER the arrival address
or the classifier fires, and nothing here can clear a restriction.
"""

from __future__ import annotations

import pytest

from app.restriction import (
    FLAG_SAFEGUARDING,
    ROUTE_EXCEPTION,
    ROUTE_SAFEGUARDING,
    apply_classification,
)


def merge(**kw):
    base = dict(current_route=None, current_restricted=False, alias_restricted=False,
                model_route="new_referral", model_flags=[])
    base.update(kw)
    return apply_classification(**base)


# ── the ordinary case ────────────────────────────────────────────────────────

def test_model_route_is_used_when_nothing_is_restricted():
    route, restricted, flags = merge()
    assert route == "new_referral"
    assert restricted is False
    assert FLAG_SAFEGUARDING not in flags


def test_model_flags_are_preserved():
    route, restricted, flags = merge(model_flags=["urgent"])
    assert flags == ["urgent"]


# ── the classifier raises it ─────────────────────────────────────────────────

def test_safeguarding_flag_forces_restriction_and_route():
    """info@ mail the model flags is as restricted as mail to safeguarding@."""
    route, restricted, flags = merge(model_flags=[FLAG_SAFEGUARDING])
    assert route == ROUTE_SAFEGUARDING
    assert restricted is True


def test_safeguarding_route_from_the_model_forces_restriction():
    route, restricted, _ = merge(model_route=ROUTE_SAFEGUARDING)
    assert restricted is True
    assert route == ROUTE_SAFEGUARDING


def test_other_flags_survive_alongside_the_safeguarding_flag():
    """A complaint that discloses a concern is still a complaint."""
    _, _, flags = merge(model_route="complaint", model_flags=["urgent", FLAG_SAFEGUARDING])
    assert "urgent" in flags and FLAG_SAFEGUARDING in flags


# ── the arrival address raises it — and the model cannot overrule ───────────

def test_alias_restriction_survives_a_model_that_disagrees():
    """THE regression this module exists for: mail to safeguarding@ classified as
    something mundane must not be downgraded."""
    route, restricted, flags = merge(
        alias_restricted=True, model_route="general_enquiry", model_flags=[])
    assert restricted is True
    assert route == ROUTE_SAFEGUARDING
    assert FLAG_SAFEGUARDING in flags, "the reason must be visible on the row"


def test_existing_restriction_survives_reclassification():
    route, restricted, _ = merge(
        current_restricted=True, model_route="general_enquiry")
    assert restricted is True
    assert route == ROUTE_SAFEGUARDING


def test_restriction_never_downgrades_even_with_empty_model_output():
    route, restricted, _ = merge(
        current_restricted=True, model_route=None, model_flags=None)
    assert restricted is True


@pytest.mark.parametrize("model_route", [None, "", "general_enquiry", "complaint",
                                         "payment_or_finance", "new_referral"])
def test_no_model_route_can_clear_an_alias_restriction(model_route):
    _, restricted, _ = merge(alias_restricted=True, model_route=model_route)
    assert restricted is True


# ── the exception queue ──────────────────────────────────────────────────────

def test_unresolved_ingestion_keeps_the_item_in_the_exception_queue():
    """The classifier never saw the ambiguity, so it cannot resolve it."""
    route, restricted, _ = merge(
        current_route=ROUTE_EXCEPTION, model_route="new_referral")
    assert route == ROUTE_EXCEPTION
    assert restricted is False


def test_restriction_outranks_the_exception_queue():
    """Restricted material is routed restricted even when the address was unclear."""
    route, restricted, _ = merge(
        current_route=ROUTE_EXCEPTION, alias_restricted=True,
        model_route="new_referral")
    assert route == ROUTE_SAFEGUARDING
    assert restricted is True


def test_falls_back_to_exception_when_nothing_has_an_opinion():
    route, restricted, _ = merge(model_route=None, current_route=None)
    assert route == ROUTE_EXCEPTION
    assert restricted is False


def test_ingestion_route_is_kept_when_the_model_offers_none():
    route, _, _ = merge(current_route="payment_or_finance", model_route=None)
    assert route == "payment_or_finance"


# ── idempotence ──────────────────────────────────────────────────────────────

def test_reapplying_the_same_result_is_stable():
    r1, s1, f1 = merge(alias_restricted=True, model_route="complaint")
    r2, s2, f2 = apply_classification(
        current_route=r1, current_restricted=s1, alias_restricted=True,
        model_route="complaint", model_flags=f1)
    assert (r1, s1) == (r2, s2)
    assert f2.count(FLAG_SAFEGUARDING) == 1, "flag must not accumulate on re-analysis"
