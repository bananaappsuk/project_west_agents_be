"""HTTP surface for the shared orchestration layer.

A separate router from `api.py` on purpose. These routes are capability-agnostic
in shape -- start a workflow, list the review queue, approve, reject, inspect the
trail -- and the Voice Agent's equivalent will look the same, so keeping them apart
from the mail-specific routes makes the shared interface visible as a shared
interface rather than something buried among email endpoints.

Every route resolves the caller's mailbox scope through the existing
`deps.mailbox_scope` dependency and hands it to the orchestrator as an
authorization port. Nothing here decides authorization itself; it only refuses to
run without one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from platform_orchestration import (
    InvalidTransition,
    NotAuthorized,
    WorkflowNotFound,
    WorkflowState,
)

from . import orchestration
from .config import settings
from .deps import mailbox_scope, require
from .mailbox_scope import MailboxScope

router = APIRouter(prefix="/workflows", tags=["workflows"])

READ = f"{settings.app_key}:emails.read"
WRITE = f"{settings.app_key}:emails.write"

#: Sent for every "you may not" outcome, whether the run is absent or merely not
#: this caller's to see. Deliberately indistinguishable -- a 403 on a read would
#: confirm the run exists and that somebody else can see it, which for
#: safeguarding material is itself a disclosure. The same decision `_get_email`
#: already takes in `api.py`.
_NOT_FOUND = "not found"


class StartReplyApprovalIn(BaseModel):
    email_id: str
    #: The reviewer's edited text. Omitted means "use the stored draft", which is
    #: what the Approve-as-drafted path does.
    body: str | None = Field(default=None, max_length=100_000)


class DecisionIn(BaseModel):
    note: str | None = Field(default=None, max_length=2_000)


def _serialize_run(run) -> dict:
    return {
        "id": run.id,
        "definition": run.definition,
        "capability": run.capability,
        "subjectRef": run.subject_ref,
        "state": run.state.value,
        "stepIndex": run.step_index,
        "attempts": run.attempts,
        "queue": run.queue,
        "routingRule": run.routing_rule,
        "startedBy": run.started_by,
        "lastError": run.last_error,
        "nextAttemptAt": run.next_attempt_at,
        "createdAt": run.created_at,
        "updatedAt": run.updated_at,
        "finishedAt": run.finished_at,
        # Routing annotations and context provenance. Non-sensitive by
        # construction -- the engine only ever puts references here.
        "metadata": run.metadata,
    }


def _serialize_step(step) -> dict:
    return {
        "name": step.name,
        "state": step.state.value,
        "attempts": step.attempts,
        "error": step.error,
        "startedAt": step.started_at,
        "finishedAt": step.finished_at,
    }


def _orchestrator_for(scope: MailboxScope):
    return orchestration.build_orchestrator(
        orchestration.MailboxAuthorizationPort(scope)
    )


@router.post("/reply-approval", status_code=status.HTTP_201_CREATED)
async def start_reply_approval(
    body: StartReplyApprovalIn,
    claims: dict = Depends(require(WRITE)),
    scope: MailboxScope = Depends(mailbox_scope),
):
    """Put an agent-drafted reply into the approval queue.

    Returns the existing run when one is already open for this email: the
    idempotency key is the email id, so a double-clicked button or a repeated
    call produces one review item rather than several for the same message.

    The run is advanced as far as it can go, which for this definition means
    running `prepare` and then stopping at the approval gate. Nothing is sent.
    """
    try:
        run = await orchestration.submit_for_approval(
            email_id=body.email_id,
            body=body.body,
            principal=orchestration.principal_from_claims(claims),
            scope=scope,
        )
    except NotAuthorized:
        # Not a 403: the caller must not learn whether this email exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from None

    return _serialize_run(run)


@router.get("/review-queue")
async def review_queue(
    claims: dict = Depends(require(READ)),
    scope: MailboxScope = Depends(mailbox_scope),
    limit: int = 100,
):
    """Workflows waiting for a human decision, within this user's own scope.

    A user authorised for no mailbox gets an explicit error rather than an empty
    list -- an empty queue and "you may see nothing" look identical otherwise, and
    the existing mailbox-scope work is deliberate that the difference must be
    stated. Items are then filtered again by the orchestrator's own port, which
    is what applies safeguarding clearance.
    """
    if scope.is_empty:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "you are not authorised for any mailbox, so no review queue exists for you",
        )

    orch = _orchestrator_for(scope)
    principal = orchestration.principal_from_claims(claims)
    runs = await orch.list_runs(
        principal=principal,
        capability=orchestration.CAPABILITY,
        states=[WorkflowState.AWAITING_APPROVAL],
        limit=limit,
    )
    return {"runs": [_serialize_run(r) for r in runs]}


@router.get("/{run_id}")
async def get_run(
    run_id: str,
    claims: dict = Depends(require(READ)),
    scope: MailboxScope = Depends(mailbox_scope),
):
    orch = _orchestrator_for(scope)
    principal = orchestration.principal_from_claims(claims)
    try:
        run = await orch.get(run_id, principal=principal)
        steps = await orch.steps(run_id, principal=principal)
    except WorkflowNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from None
    return {**_serialize_run(run), "steps": [_serialize_step(s) for s in steps]}


@router.post("/{run_id}/approve")
async def approve(
    run_id: str,
    body: DecisionIn,
    claims: dict = Depends(require(WRITE)),
    scope: MailboxScope = Depends(mailbox_scope),
):
    """Authorise the gated step and let the workflow carry on.

    The approval is recorded before anything acts on it, so a send that happens
    always has a durable record of who allowed it. Requires the `can_approve`
    grant on the mailbox -- reading it is not enough.
    """
    orch = _orchestrator_for(scope)
    principal = orchestration.principal_from_claims(claims)
    try:
        run = await orch.approve_and_continue(
            run_id, principal=principal, approved=True, note=body.note,
        )
    except WorkflowNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from None
    except NotAuthorized:
        # The caller can already see this run, so there is nothing left to conceal
        # by pretending otherwise -- 403 is the honest answer here.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "you are not authorised to approve this"
        ) from None
    except InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _serialize_run(run)


@router.post("/{run_id}/reject")
async def reject(
    run_id: str,
    body: DecisionIn,
    claims: dict = Depends(require(WRITE)),
    scope: MailboxScope = Depends(mailbox_scope),
):
    """Decline the gated step. The decision is recorded, not merely implied."""
    orch = _orchestrator_for(scope)
    principal = orchestration.principal_from_claims(claims)
    try:
        run = await orch.submit_approval(
            run_id, principal=principal, approved=False, note=body.note,
        )
    except WorkflowNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from None
    except NotAuthorized:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "you are not authorised to approve this"
        ) from None
    except InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _serialize_run(run)


@router.post("/{run_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel(
    run_id: str,
    body: DecisionIn,
    claims: dict = Depends(require(WRITE)),
    scope: MailboxScope = Depends(mailbox_scope),
):
    orch = _orchestrator_for(scope)
    principal = orchestration.principal_from_claims(claims)
    try:
        await orch.cancel(run_id, principal=principal, reason=body.note or "")
    except WorkflowNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from None
    except NotAuthorized:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "you are not authorised to cancel this"
        ) from None
    except InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
