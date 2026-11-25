"""Orchestration failure modes, as distinct exception types.

Each of these means something different to a caller, and collapsing any two of
them would lose a distinction the API layer has to act on: an invalid transition
is a programming error (500), an authorization failure must not even confirm the
workflow exists (404), and an approval requirement is a normal, expected outcome
that the UI renders as a review queue rather than an error.
"""

from __future__ import annotations


class OrchestrationError(Exception):
    """Base class, so a host can catch everything from this layer at once."""


class InvalidTransition(OrchestrationError):
    """A state change the lifecycle does not permit.

    Raised rather than silently coerced. A workflow that can be pushed from
    `rejected` back into `running` is a workflow whose approval gate means
    nothing, so the illegal move has to fail loudly at the point it is attempted.
    """

    def __init__(self, frm: str, to: str) -> None:
        super().__init__(f"illegal workflow transition: {frm} -> {to}")
        self.frm = frm
        self.to = to


class WorkflowNotFound(OrchestrationError):
    """No such workflow run — or none this principal is allowed to know about.

    Deliberately the same exception for both, mirroring the decision already
    taken in the mail agent's `MailboxForbidden`: a 403 confirms the record
    exists and that somebody else can see it, which for safeguarding material is
    itself a disclosure. An unauthorised read is indistinguishable from a
    missing row.
    """

    status_code = 404
    detail = "not found"


class NotAuthorized(OrchestrationError):
    """The principal may not take this action on this workflow.

    Used for *actions* (approve, start) where the principal is already entitled
    to see the workflow, so there is nothing left to conceal by pretending it is
    absent. A read they are not entitled to raises `WorkflowNotFound` instead.
    """

    status_code = 403
    detail = "not authorized"


class UnknownHandler(OrchestrationError):
    """A step names a handler key nothing has registered.

    Caught at definition-registration time wherever possible — a workflow that
    only discovers its third step is unroutable after performing the first two
    has already had side effects it cannot take back.
    """


class DefinitionError(OrchestrationError):
    """A workflow definition that cannot be executed as written."""
