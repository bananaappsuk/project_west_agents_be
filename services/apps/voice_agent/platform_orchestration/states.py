"""The workflow lifecycle: the states a run can occupy and the moves between them.

One table, `TRANSITIONS`, is the whole lifecycle. Every state change in the engine
goes through `assert_transition`, so a new state or a new edge is a change here and
nowhere else — and an edge nobody intended cannot be introduced by a well-meaning
assignment somewhere in a capability's own code.

Why these particular states:

  * `AWAITING_APPROVAL` is a first-class state rather than a boolean on the run.
    The requirement is that a consequential action must not execute before a human
    has approved it; a flag alongside a `running` state would still be a running
    workflow, and the thing that stops execution needs to be the state itself.

  * `RETRY_SCHEDULED` is separate from `RUNNING` so that "waiting to be retried"
    is distinguishable from "being worked on right now". Without the distinction a
    recovery sweep cannot tell a run it should pick up from one another process
    already holds.

  * `EXCEPTION` is separate from `FAILED`. `FAILED` means the workflow is finished
    and did not succeed; `EXCEPTION` means it stopped and needs a person to look at
    it, and can still be resumed. Conflating them either buries work that needs
    attention or leaves genuinely dead runs looking actionable forever. This mirrors
    the `exception` queue the Project West taxonomy already routes to.
"""

from __future__ import annotations

from enum import Enum

from .errors import InvalidTransition


class WorkflowState(str, Enum):
    """`str`-valued so a state persists and serialises as its own name.

    Storing the stable string rather than an ordinal means the database column is
    readable in a query and reordering this enum can never silently re-interpret
    historical rows.
    """

    CREATED = "created"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXCEPTION = "exception"
    CANCELLED = "cancelled"


S = WorkflowState

#: The complete lifecycle. A state absent from a value tuple is unreachable from
#: that key, by design rather than by omission.
TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    # EXCEPTION is reachable straight from CREATED: a run can be unexecutable
    # before its first step ever runs -- the definition it was started against
    # having moved on under it is the case that actually occurs. It has to park
    # somewhere a person will see, and "failed" would both be a lie and be
    # terminal, stranding work that is only waiting on a redeploy.
    S.CREATED: frozenset({S.RUNNING, S.EXCEPTION, S.CANCELLED}),
    # RUNNING -> RUNNING is legal and load-bearing: it is how a multi-step workflow
    # advances from one step to the next without passing through an idle state that
    # a recovery sweep would have to interpret.
    S.RUNNING: frozenset({
        S.RUNNING, S.AWAITING_APPROVAL, S.RETRY_SCHEDULED, S.SUCCEEDED,
        S.FAILED, S.EXCEPTION, S.CANCELLED,
    }),
    # No direct AWAITING_APPROVAL -> RUNNING edge. Approval must be *recorded*
    # (-> APPROVED) before execution resumes, so the decision and its author exist
    # durably before anything acts on them. An edge straight back to RUNNING would
    # permit resuming with no record of who allowed it.
    S.AWAITING_APPROVAL: frozenset({S.APPROVED, S.REJECTED, S.EXCEPTION, S.CANCELLED}),
    S.APPROVED: frozenset({S.RUNNING, S.CANCELLED}),
    S.RETRY_SCHEDULED: frozenset({S.RUNNING, S.FAILED, S.EXCEPTION, S.CANCELLED}),
    # EXCEPTION is recoverable: a person fixes the cause and the run continues.
    S.EXCEPTION: frozenset({S.RUNNING, S.FAILED, S.CANCELLED}),
    # Terminal. A rejected workflow does not become a running one on a second
    # thought -- a new run is started instead, so the rejection stays on record.
    S.SUCCEEDED: frozenset(),
    S.FAILED: frozenset(),
    S.REJECTED: frozenset(),
    S.CANCELLED: frozenset(),
}

#: States from which nothing further happens. Derived from the table rather than
#: listed again, so the two can never disagree.
TERMINAL: frozenset[WorkflowState] = frozenset(
    state for state, allowed in TRANSITIONS.items() if not allowed
)

#: States where the engine is waiting on something outside itself -- a person, or
#: the clock. Not terminal, but not progressable without an external event either.
WAITING: frozenset[WorkflowState] = frozenset({
    S.AWAITING_APPROVAL, S.RETRY_SCHEDULED, S.EXCEPTION,
})


def can_transition(frm: WorkflowState, to: WorkflowState) -> bool:
    return to in TRANSITIONS.get(frm, frozenset())


def assert_transition(frm: WorkflowState, to: WorkflowState) -> None:
    """Raise unless the move is permitted. The engine's only way to change state."""
    if not can_transition(frm, to):
        raise InvalidTransition(frm.value, to.value)


def is_terminal(state: WorkflowState) -> bool:
    return state in TERMINAL
