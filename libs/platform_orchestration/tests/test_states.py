"""The lifecycle table itself.

These are the cheapest tests in the suite and they guard the most consequential
property: that the approval gate cannot be walked around by a state change
somebody adds later. A new edge in `TRANSITIONS` that breaks one of these is
meant to fail here rather than be discovered by something being sent.
"""

from __future__ import annotations

import pytest

from platform_orchestration import (
    TERMINAL,
    TRANSITIONS,
    InvalidTransition,
    WorkflowState,
    assert_transition,
    can_transition,
    is_terminal,
)

S = WorkflowState


# ── the happy path ───────────────────────────────────────────────────────────

def test_the_ordinary_route_through_a_workflow():
    assert can_transition(S.CREATED, S.RUNNING)
    assert can_transition(S.RUNNING, S.SUCCEEDED)


def test_running_may_advance_to_running_for_the_next_step():
    """A multi-step run moves step to step without passing through an idle state."""
    assert can_transition(S.RUNNING, S.RUNNING)


def test_the_full_approval_route():
    assert can_transition(S.RUNNING, S.AWAITING_APPROVAL)
    assert can_transition(S.AWAITING_APPROVAL, S.APPROVED)
    assert can_transition(S.APPROVED, S.RUNNING)


def test_the_full_retry_route():
    assert can_transition(S.RUNNING, S.RETRY_SCHEDULED)
    assert can_transition(S.RETRY_SCHEDULED, S.RUNNING)
    assert can_transition(S.RETRY_SCHEDULED, S.FAILED)


def test_an_exception_is_recoverable():
    """`exception` means "a person must look at this", not "this is over"."""
    assert can_transition(S.EXCEPTION, S.RUNNING)
    assert not is_terminal(S.EXCEPTION)


# ── what must never be possible ──────────────────────────────────────────────

def test_approval_cannot_be_skipped():
    """The gate's entire value: no edge from waiting straight back to executing.

    Without this, a run could resume with no record of who allowed it.
    """
    assert not can_transition(S.AWAITING_APPROVAL, S.RUNNING)


def test_a_rejected_workflow_never_runs():
    for state in (S.RUNNING, S.APPROVED, S.AWAITING_APPROVAL, S.SUCCEEDED):
        assert not can_transition(S.REJECTED, state)


def test_terminal_states_are_terminal():
    assert TERMINAL == frozenset({S.SUCCEEDED, S.FAILED, S.REJECTED, S.CANCELLED})
    for state in TERMINAL:
        assert TRANSITIONS[state] == frozenset()
        assert is_terminal(state)


def test_a_finished_workflow_cannot_be_restarted():
    assert not can_transition(S.SUCCEEDED, S.RUNNING)
    assert not can_transition(S.FAILED, S.RUNNING)
    assert not can_transition(S.CANCELLED, S.RUNNING)


def test_created_cannot_jump_past_execution():
    for state in (S.SUCCEEDED, S.AWAITING_APPROVAL, S.APPROVED, S.FAILED):
        assert not can_transition(S.CREATED, state)


# ── the enforcement helper ───────────────────────────────────────────────────

def test_assert_transition_allows_a_legal_move():
    assert assert_transition(S.CREATED, S.RUNNING) is None


def test_assert_transition_raises_on_an_illegal_move():
    with pytest.raises(InvalidTransition) as exc:
        assert_transition(S.REJECTED, S.RUNNING)
    assert exc.value.frm == "rejected"
    assert exc.value.to == "running"


def test_every_state_appears_in_the_table():
    """A state added to the enum but not the table would be a dead end nobody
    declared -- and `assert_transition` would refuse to move out of it."""
    assert set(TRANSITIONS) == set(S)


def test_cancellation_is_reachable_from_every_non_terminal_state():
    """Operationally required: any run that is still alive can be stopped."""
    for state, allowed in TRANSITIONS.items():
        if state in TERMINAL:
            continue
        assert S.CANCELLED in allowed, "%s cannot be cancelled" % state.value
