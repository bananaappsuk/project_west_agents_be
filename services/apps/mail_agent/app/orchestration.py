"""The mail agent's wiring into the shared orchestration foundation.

This module is the whole of the mail agent's coupling to `platform_orchestration`:
the authorization adapter, the step handlers, the workflow definitions and the
routing table. The engine imports nothing from here, and nothing here reimplements
anything the engine provides -- which is what lets the Voice Agent arrive later with
its own equivalent of this file and no change to the shared core.

The gap this closes
-------------------

The mail agent already drafts replies and already has a reject path
(`/emails/{id}/reply/dismiss`), but sending has never been gated: `POST
/emails/{id}/reply` sends immediately, and `EmailAudit` is written in exactly one
place -- `ingest.py`, action `"ingested"`. Despite the model's docstring listing
`approved`, `rejected`, `sent` and `send_failed`, no code path writes them, so there
is today no record of who authorised an outbound reply.

The `mail.reply_approval` workflow below is that record, and that gate.

Every outbound path now goes through it. There were two others, and neither sends
directly any more:

  * `POST /emails/{id}/reply` -- the review UI's Send button. It no longer sends; it
    queues the reply for approval and returns the run. Sending takes the
    `can_approve` grant on the mailbox, which is a distinct axis from `can_send`,
    so a user who may draft is no longer automatically a user who may release.
  * the pipeline's autonomous auto-reply (`pipeline.maybe_send`), which fires when
    an administrator has switched auto-reply on. That setting is itself an
    authorisation decision, so it is now recorded as one against every run it
    releases -- `mail.auto_reply` below, approved by the standing policy rather
    than by nobody. The eligibility rules it was already subject to are unchanged
    and still live in the pipeline; what changed is that the send is now gated,
    idempotent and audited like any other.

`mail_client.send_reply` / `graph_client.send_reply` remain the only functions that
touch a mail host, and `mail.send_reply` is now their only caller in application
code.

What is deliberately NOT here
-----------------------------

Classification routing. The agent factory decides an email's taxonomy queue next to
the taxonomy that defines it, on purpose, so that the controlled vocabulary has one
home rather than a vendored copy per service. This module therefore takes the route
the agent already decided as an input fact and never recomputes it. What the routing
table here decides is a different question the taxonomy does not answer: given a
queue, is an agent-drafted reply something a human may approve at all.
"""

from __future__ import annotations

import logging

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from platform_orchestration import (
    SYSTEM_PRINCIPAL,
    TERMINAL,
    DefinitionRegistry,
    HandlerRegistry,
    Orchestrator,
    Principal,
    RoutingRule,
    RoutingTable,
    RoutingTarget,
    StepResult,
    StepSpec,
    WorkflowDefinition,
    WorkflowState,
)
from platform_orchestration.audit import FanOutAuditSink, LoggingAuditSink
from platform_orchestration.errors import (
    InvalidTransition,
    NotAuthorized,
    WorkflowNotFound,
)
from platform_orchestration.sql import SqlAuditSink, SqlWorkflowRepository

from . import crypto, pipeline, receiving_address, restriction, send_identity
from .db import SessionLocal
from .mailbox_scope import MailboxScope
from .models import Email, EmailAudit, Mailbox, MailboxAlias

log = logging.getLogger("mail_agent.orchestration")

#: This service's capability key, as the shared layer knows it.
CAPABILITY = "mail"

#: The gated outbound-reply workflow: a person approves, then it sends.
REPLY_APPROVAL = "mail.reply_approval"

#: The autonomous outbound-reply workflow. Identical steps and identical
#: safeguards; what differs is who authorises the send -- the org's standing
#: auto-reply setting rather than a reviewer. Separate definitions rather than one
#: with a flag, so "which runs did a person approve?" is answerable by definition
#: name, and so a policy approval can never reach a run started from the review UI.
AUTO_REPLY = "mail.auto_reply"

#: Recorded as the approving authority on an auto-reply run, namespaced by the
#: engine to `policy:mail.auto_reply_enabled`. Names the setting an administrator
#: actually turned on, so the audit row points at a decision someone made.
AUTO_REPLY_POLICY = "mail.auto_reply_enabled"


# ─────────────────────────────────────────────────────────────────────────────
# Authorization adapter
# ─────────────────────────────────────────────────────────────────────────────

class MailboxAuthorizationPort:
    """`AuthorizationPort` over the mail agent's existing `MailboxScope`.

    All three answers reduce to the same question: which mailbox does this email
    belong to, and does the signed-in user hold the relevant grant on it? The
    scope is resolved per request from the verified token's subject and group
    claims and is never derived from anything in the request body -- this adapter
    only consumes it.

    Two properties carried over from the existing authorization work rather than
    re-decided here:

      * **Safeguarding clearance is a separate axis.** A restricted email is
        invisible without clearance even to a user who holds every mailbox grant,
        because `safeguarding@` is an alias into `coordinator@` which every named
        user can read. Mailbox access alone must not reveal it.
      * **Failing closed includes failing on absence.** An email with no
          `mailbox_id`, or one that no longer exists, is refused rather than
          treated as unscoped.
    """

    def __init__(self, scope: MailboxScope) -> None:
        self._scope = scope

    async def _email(self, subject_ref: str) -> Email | None:
        async with SessionLocal() as session:
            return await session.get(Email, subject_ref)

    async def _permitted(self, subject_ref: str, grant: str) -> bool:
        email = await self._email(subject_ref)
        if email is None or not email.mailbox_id:
            # No mailbox means nothing to check the grant against. Refusing is the
            # only safe reading: an unscoped row must not be universally readable.
            return False

        if email.mailbox_id not in self._scope.readable:
            return False

        # Restricted material needs clearance on top of mailbox access, whichever
        # grant was asked for -- including approval. Someone who cannot see an
        # item is in no position to authorise an action on it.
        if bool(email.restricted) and not self._scope.visible_restricted():
            return False

        if grant == "read":
            return True
        if grant == "send":
            return email.mailbox_id in self._scope.sendable
        if grant == "approve":
            return email.mailbox_id in self._scope.approvable
        return False

    async def can_view(
        self, principal: Principal, *, capability: str, subject_ref: str
    ) -> bool:
        return await self._permitted(subject_ref, "read")

    async def can_start(
        self, principal: Principal, *, capability: str, subject_ref: str
    ) -> bool:
        # Starting a reply-approval workflow is an assertion that this reply should
        # go out, so it takes the send grant and not merely read. The workflow
        # still cannot send without a separate approval.
        return await self._permitted(subject_ref, "send")

    async def can_approve(
        self, principal: Principal, *, capability: str, subject_ref: str
    ) -> bool:
        return await self._permitted(subject_ref, "approve")


class SystemAuthorizationPort:
    """The port the background scheduler runs under.

    The cron has no user identity, so it cannot be given a `MailboxScope`. It is
    allowed to *start* and *advance* workflows -- that is its job -- and is
    refused approval outright, which the engine enforces independently by
    rejecting any non-human principal. Both checks exist because this one is
    about what the scheduler is for, and the engine's is about what an approval
    means.
    """

    async def can_view(
        self, principal: Principal, *, capability: str, subject_ref: str
    ) -> bool:
        return principal.service

    async def can_start(
        self, principal: Principal, *, capability: str, subject_ref: str
    ) -> bool:
        return principal.service

    async def can_approve(
        self, principal: Principal, *, capability: str, subject_ref: str
    ) -> bool:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Step handlers -- the mail agent's own business logic
# ─────────────────────────────────────────────────────────────────────────────

handlers = HandlerRegistry()


@handlers.handler("mail.prepare_reply")
async def prepare_reply(ctx) -> StepResult:
    """Check this reply may be sent at all, before a human is asked to approve it.

    Every refusal here is a `fail`, not a `retry`: none of them can be fixed by
    trying again, and a retry would only put the same question to the reviewer
    three times. The checks reproduce no policy of their own -- they read the
    decisions ingestion and classification already recorded on the row, and they
    refuse with `send_identity`'s reason strings rather than a second vocabulary,
    because that module is where this service already decided what makes a reply
    unsendable.

    Two of those refusals apply to the agent acting alone and not to a person:

      * restricted material -- `RESTRICTED_NO_AUTONOMOUS` is exactly that
        distinction, and a person who gets this far has already been checked for
        safeguarding clearance by `MailboxAuthorizationPort`, twice (to queue it
        and again to approve it).
      * an unresolved receiving address -- `RESOLVED_STATUSES` says such a row
        "must not drive autonomous behaviour". Refusing a human as well would make
        every pre-PW `legacy` row permanently unanswerable, which is a capability
        the gate is not entitled to remove.

    What this does *not* do is derive the From address. `send_identity.derive`
    decides that too, and nothing in the service calls it yet; wiring it changes
    which identity outbound mail carries and needs Exchange SendAs configured
    first, so it is deliberately left alone here.
    """
    autonomous = bool(ctx.payload.get("auto"))

    async with SessionLocal() as session:
        email = await session.get(Email, ctx.subject_ref)
        if email is None:
            return StepResult.fail("email no longer exists")

        body = (ctx.payload.get("body") or email.draft_reply or "").strip()
        if not body:
            return StepResult.fail("no reply body to send")

        if email.reply_status == "sent":
            # Not a failure to be retried: the reply is already out. Succeeding
            # here with nothing to do would let the send step run again.
            return StepResult.fail("a reply has already been sent for this email")

        restricted = bool(email.restricted) or email.route == restriction.ROUTE_SAFEGUARDING
        if restricted and autonomous:
            return StepResult.fail(send_identity.RESTRICTED_NO_AUTONOMOUS)

        if autonomous and (
            email.receiving_address_status not in receiving_address.RESOLVED_STATUSES
            or email.route == restriction.ROUTE_EXCEPTION
        ):
            return StepResult.fail(send_identity.UNRESOLVED_RECEIVING_ADDRESS)

        mailbox = await session.get(Mailbox, email.mailbox_id) if email.mailbox_id else None
        if mailbox is None:
            return StepResult.fail(send_identity.NO_MAILBOX)

        # An alias may forbid replying from it at all -- safeguarding@ is
        # configured this way, and that holds independently of clearance.
        if email.receiving_address:
            alias = await session.scalar(
                select(MailboxAlias).where(
                    MailboxAlias.address == email.receiving_address
                )
            )
            if alias is not None and not alias.reply_allowed:
                return StepResult.fail(send_identity.ALIAS_REPLY_FORBIDDEN)

        return StepResult.ok(
            to_addr=email.from_email,
            subject="Re: %s" % email.subject,
            body=body,
            mailbox_id=email.mailbox_id,
            provider=mailbox.provider,
            # Recorded so a reviewer's decision and an investigator's reading of it
            # both show whether this was restricted material, which the run's own
            # state otherwise does not say.
            restricted=restricted,
        )


@handlers.handler("mail.send_reply")
async def send_reply(ctx) -> StepResult:
    """Actually send. Reached only after a human approval is on record.

    The engine guarantees that: the step is declared `requires_approval`, so its
    handler is not called until an `ApprovalRecord` exists. This handler does not
    re-check -- a second, weaker copy of that rule here would be the thing that
    rots.

    Delivery goes through `pipeline._send_fn_for`, the same chooser the existing
    manual reply route uses, so a Graph mailbox sends via Graph and an IMAP one via
    SendGrid or SMTP exactly as it does today.
    """
    prepared = ctx.output_of("prepare")
    if not prepared:
        return StepResult.fail("nothing prepared to send")

    async with SessionLocal() as session:
        mailbox = await session.get(Mailbox, prepared["mailbox_id"])
        if mailbox is None:
            return StepResult.fail("mailbox disappeared before the reply was sent")
        try:
            send_creds = _send_creds(mailbox)
        except MissingCredential as exc:
            # Permanent until someone fixes the mailbox configuration, so `fail`
            # rather than `retry`: three attempts would find the same gap.
            return StepResult.fail(str(exc))

    # Who actually released this send, read from the approval record rather than
    # inferred from how the run was started. `auto_sent` means "went out without a
    # person pressing send", so the authority on record is the only thing that can
    # answer it: a human who approves an autonomous run makes it a human send, and
    # a run started by the pipeline but released by a reviewer is not an auto-send.
    # Guessing from the payload got this wrong in exactly that case.
    approval = await workflow_repository().approval_for(ctx.run_id, ctx.step.name)
    auto_sent = bool(approval and approval.decided_by.startswith("policy:"))

    send_fn = pipeline._send_fn_for(prepared["provider"])
    try:
        await run_in_threadpool(
            send_fn,
            **send_creds,
            to_addr=prepared["to_addr"],
            subject=prepared["subject"],
            body=prepared["body"],
        )
    except Exception as exc:
        # Transient by default. A mail host refusing a connection, a token that
        # needs refreshing and a deploy restart all land here, and all clear on a
        # retry; the step's attempt budget is what stops a genuinely broken
        # configuration retrying forever.
        return StepResult.retry("send failed: %s" % exc)

    async with SessionLocal() as session:
        email = await session.get(Email, ctx.subject_ref)
        if email is not None:
            email.reply_status = "sent"
            email.auto_sent = auto_sent
            await session.commit()

    return StepResult.ok(sent=True, to_addr=prepared["to_addr"])


class MissingCredential(Exception):
    """A mailbox row has no credential for the provider it claims to use.

    Raised rather than letting `None` reach Fernet, which answers a missing
    secret with a type error from inside the crypto library -- a confusing thing
    to find in a log when the actual fault is an incompletely configured mailbox.
    The send step turns this into a step failure with a readable reason.
    """


def _send_creds(mailbox: Mailbox) -> dict:
    """The provider's credentials, shaped as its send function expects.

    `client_secret_enc` is nullable (an IMAP mailbox has no Graph secret), so the
    Graph branch has to say what it needs rather than assume it is there. mypy
    found this: the column is `str | None` and `crypto.decrypt` takes `str`.
    """
    if mailbox.provider == "graph":
        if not mailbox.client_secret_enc:
            raise MissingCredential(
                "mailbox %s is configured for graph but has no client secret"
                % mailbox.id
            )
        return {
            "tenant_id": mailbox.tenant_id,
            "client_id": mailbox.client_id,
            "client_secret": crypto.decrypt(mailbox.client_secret_enc),
            "mailbox": mailbox.username,
        }
    if not mailbox.password_enc:
        raise MissingCredential(
            "mailbox %s is configured for imap but has no password" % mailbox.id
        )
    return {
        "smtp_host": mailbox.smtp_host,
        "smtp_port": mailbox.smtp_port,
        "username": mailbox.username,
        "password": crypto.decrypt(mailbox.password_enc),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Workflow definitions
# ─────────────────────────────────────────────────────────────────────────────

definitions = DefinitionRegistry()

definitions.register(
    WorkflowDefinition(
        name=REPLY_APPROVAL,
        capability=CAPABILITY,
        description=(
            "Prepare an agent-drafted reply, hold it for human approval, then send "
            "it and record who authorised it."
        ),
        steps=(
            StepSpec(
                name="prepare",
                handler="mail.prepare_reply",
                # One attempt: every refusal this step can return is permanent, so
                # a retry budget would only repeat the same answer.
                max_attempts=1,
            ),
            StepSpec(
                name="send",
                handler="mail.send_reply",
                requires_approval=True,
                consequential=True,
                # Three attempts for the network, matching the CRM client's own
                # transient-failure budget.
                max_attempts=3,
            ),
        ),
    ),
    known_handlers=handlers.keys(),
)

definitions.register(
    WorkflowDefinition(
        name=AUTO_REPLY,
        capability=CAPABILITY,
        description=(
            "Send an agent-drafted reply autonomously, under the org's standing "
            "auto-reply setting, recording that setting as the authority."
        ),
        steps=(
            StepSpec(
                name="prepare",
                handler="mail.prepare_reply",
                max_attempts=1,
            ),
            StepSpec(
                name="send",
                handler="mail.send_reply",
                # Still gated. The gate is satisfied by the standing policy rather
                # than by a reviewer, but it is the same gate: the handler is not
                # called until an approval record exists, so an auto-send that
                # happens always has an authority recorded against it.
                requires_approval=True,
                consequential=True,
                policy_approval=True,
                max_attempts=3,
            ),
        ),
    ),
    known_handlers=handlers.keys(),
)


# ─────────────────────────────────────────────────────────────────────────────
# The two ways a reply leaves this service
# ─────────────────────────────────────────────────────────────────────────────

async def submit_for_approval(
    *, email_id: str, body: str | None, principal: Principal, scope: MailboxScope
):
    """Queue a reply for human approval and advance it to the gate. Sends nothing.

    Shared by `POST /emails/{id}/reply` and `POST /workflows/reply-approval` so the
    two cannot drift into different rules about who may queue what. The idempotency
    key is the email id, so a double-clicked Send button produces one review item.

    Raises `NotAuthorized` when the caller lacks the send grant on the email's
    mailbox, which both routes translate into a 404 -- the caller must not learn
    whether the email exists.
    """
    orch = build_orchestrator(MailboxAuthorizationPort(scope))
    run = await orch.start(
        definition=REPLY_APPROVAL,
        subject_ref=email_id,
        principal=principal,
        payload={"body": body} if body else {},
        idempotency_key=email_id,
    )
    return await orch.run_to_completion(run.id, principal=principal)


async def reject_open_run(
    *, email_id: str, principal: Principal, scope: MailboxScope, note: str | None = None
) -> bool:
    """Record a rejection on this email's open approval run, if it has one.

    Returns whether a decision was recorded. Deliberately quiet about everything
    else: it is called from the dismiss route to keep the two review surfaces
    consistent, and a dismissal must not fail because there was no run to close or
    because this user may dismiss but not approve.
    """
    run = await workflow_repository().find_by_idempotency_key(
        definition=REPLY_APPROVAL, idempotency_key=email_id
    )
    if run is None or run.state is not WorkflowState.AWAITING_APPROVAL:
        return False

    orch = build_orchestrator(MailboxAuthorizationPort(scope))
    try:
        await orch.submit_approval(
            run.id, principal=principal, approved=False, note=note,
        )
    except (NotAuthorized, InvalidTransition, WorkflowNotFound):
        # The row is already marked rejected; leaving the run for an approver to
        # see is strictly safer than failing the dismissal. Nothing can send from
        # here either way -- the gate has no approval on record.
        log.info("dismissed email %s but could not close run %s", email_id, run.id)
        return False
    return True


async def auto_send_reply(email_id: str, *, body: str) -> bool:
    """Send an auto-reply under the standing policy. Returns whether it went out.

    Called from the analysis pipeline, which has already applied every eligibility
    rule it applied before (the org setting, the classifier's `auto_sendable`,
    `needs_reply`, and both safeguarding signals). None of those moved here: this
    function's job is to make the send gated, idempotent and audited, not to
    re-decide whether it should happen.

    The reply text is carried on the run's payload rather than read back from the
    row, because during a batch the analysis has not been persisted yet -- the
    draft only reaches the database after the send loop. Returning False on any
    refusal keeps the caller's existing contract: a reply that does not go out
    lands as a draft for a human instead.
    """
    # A reply already waiting for a person is that person's to decide. Sending
    # autonomously over the top of it would both double-send and quietly answer a
    # question a reviewer was asked -- so the human path wins. The engine's own
    # idempotency only covers repeats of the *same* definition, which is why this
    # cross-check is here and not left to it.
    pending = await workflow_repository().find_by_idempotency_key(
        definition=REPLY_APPROVAL, idempotency_key=email_id
    )
    if pending is not None and pending.state not in TERMINAL:
        log.info(
            "auto-send withheld: run %s is already awaiting a human for email %s",
            pending.id, email_id,
        )
        return False

    orch = build_orchestrator(SystemAuthorizationPort())
    run = await orch.start(
        definition=AUTO_REPLY,
        subject_ref=email_id,
        principal=SYSTEM_PRINCIPAL,
        payload={"body": body, "auto": True},
        idempotency_key=email_id,
    )
    run = await orch.run_to_completion(run.id, principal=SYSTEM_PRINCIPAL)

    if run.state is WorkflowState.AWAITING_APPROVAL:
        run = await orch.policy_approve_and_continue(
            run.id,
            principal=SYSTEM_PRINCIPAL,
            policy=AUTO_REPLY_POLICY,
            note="auto-reply is enabled for this organisation",
        )

    return run.state is WorkflowState.SUCCEEDED


# ─────────────────────────────────────────────────────────────────────────────
# Routing
# ─────────────────────────────────────────────────────────────────────────────

#: Queues where an agent-drafted reply is never appropriate, regardless of how
#: confident the classifier was. Both come straight from decisions already
#: recorded elsewhere in this service -- `restriction.py` for the safeguarding
#: route and the exception queue -- and are listed as routing rules so the reason
#: a workflow was refused is visible in the run's own metadata.
def build_routing_table() -> RoutingTable:
    """Queue -> whether a reply workflow may run, as configuration.

    Negative priorities for the overrides, per the shared layer's convention, so
    a queue rule added later cannot accidentally take precedence over a
    restriction.
    """
    return RoutingTable(
        rules=[
            RoutingRule(
                name="safeguarding-restricted",
                target=RoutingTarget(
                    queue=restriction.ROUTE_SAFEGUARDING,
                    metadata={"reply_workflow_allowed": False},
                ),
                conditions={"restricted": True},
                priority=-100,
                terminal=True,
                reason=(
                    "safeguarding-restricted: whether to respond is a clinical "
                    "decision, so no agent-drafted reply is offered"
                ),
            ),
            RoutingRule(
                name="safeguarding-route",
                target=RoutingTarget(
                    queue=restriction.ROUTE_SAFEGUARDING,
                    metadata={"reply_workflow_allowed": False},
                ),
                conditions={"route": restriction.ROUTE_SAFEGUARDING},
                priority=-90,
                terminal=True,
                reason="routed to the restricted safeguarding queue",
            ),
            RoutingRule(
                name="exception-queue",
                target=RoutingTarget(
                    queue=restriction.ROUTE_EXCEPTION,
                    metadata={"reply_workflow_allowed": False},
                ),
                conditions={"route": restriction.ROUTE_EXCEPTION},
                priority=-80,
                terminal=True,
                reason=(
                    "in the exception queue: the business entry point was never "
                    "resolved, so a human handles it"
                ),
            ),
            RoutingRule(
                name="replyable-queue",
                target=RoutingTarget(
                    queue="",
                    workflow=REPLY_APPROVAL,
                    metadata={"reply_workflow_allowed": True},
                ),
                priority=100,
                reason="an agent-drafted reply may be prepared for human approval",
            ),
        ],
        fallback=RoutingTarget(
            queue=restriction.ROUTE_EXCEPTION,
            metadata={"reply_workflow_allowed": False},
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Mirroring into the existing per-email audit trail
# ─────────────────────────────────────────────────────────────────────────────

#: Workflow actions that correspond to an `EmailAudit.action` the existing model
#: already defines. Mapped rather than invented: the per-email trail is what the
#: SSOT asks for by name and what an investigator looks at first, and it has been
#: missing every one of these.
_EMAIL_AUDIT_ACTIONS = {
    "approval_requested": "drafted",
    "approved": "approved",
    # A standing policy releasing an auto-reply is an approval and belongs in the
    # same column, so "what authorised this send?" has one place to look. The
    # `actor_kind` and the `policy` in `detail` are what distinguish it from a
    # person, rather than a separate action nobody would think to query.
    "approved_by_policy": "approved",
    "rejected": "rejected",
    "failed": "send_failed",
}


class EmailAuditMirror:
    """Writes workflow events that concern an email into `email_audit` too.

    The workflow trail (`workflow_events`) is complete and generic; `email_audit`
    is the per-record trail the mail agent already has, indexed by email and by
    actor. Mirroring the small number of actions that mean something at the record
    level means "who approved this reply, and when?" is answerable from the email's
    own history rather than only by joining through a workflow id.

    Events arrive already redacted, so no message content can reach this table
    through here.
    """

    async def record(self, event) -> None:
        action = _EMAIL_AUDIT_ACTIONS.get(event.action)
        if action is None or not event.subject_ref:
            return
        # A send that succeeded is reported as the step succeeding, which needs
        # the step name to tell it from any other successful step.
        async with SessionLocal() as session:
            email = await session.get(Email, event.subject_ref)
            if email is None:
                return
            session.add(EmailAudit(
                email_id=event.subject_ref,
                mailbox_id=email.mailbox_id,
                action=action,
                actor_user_id=event.actor_user_id,
                actor_kind=event.actor_kind,
                detail={
                    "workflow_run": event.run_id,
                    "workflow_action": event.action,
                    "step": event.step,
                    **dict(event.detail or {}),
                },
            ))
            await session.commit()


class SentAuditMirror(EmailAuditMirror):
    """Also records the send itself, which has no one-to-one workflow action.

    `step_succeeded` on the `send` step is the moment a reply actually went out,
    and it is the row the SSOT specifically wants: the approving user, the
    identity used, and when.
    """

    async def record(self, event) -> None:
        if event.action == "step_succeeded" and event.step == "send":
            async with SessionLocal() as session:
                email = await session.get(Email, event.subject_ref)
                if email is None:
                    return
                session.add(EmailAudit(
                    email_id=event.subject_ref,
                    mailbox_id=email.mailbox_id,
                    action="sent",
                    actor_user_id=event.actor_user_id,
                    actor_kind=event.actor_kind,
                    sender_identity=(event.detail or {}).get("output", {}).get("to_addr"),
                    detail={
                        "workflow_run": event.run_id,
                        "approved": True,
                        "step": event.step,
                    },
                ))
                await session.commit()
            return
        await super().record(event)


# ─────────────────────────────────────────────────────────────────────────────
# Construction
# ─────────────────────────────────────────────────────────────────────────────

def workflow_repository():
    """This service's workflow store.

    A function rather than an expression inside `build_orchestrator` so that the
    storage port has one named seam. The service's own suite has no database
    driver, and the entry points worth testing -- `submit_for_approval`,
    `auto_send_reply`, `reject_open_run` -- are the ones that build their own
    orchestrator, which would otherwise make them reachable only against Postgres.
    Substituting `InMemoryWorkflowRepository` here is what lets a test exercise
    them; it is the same port either way (asserted in the library's
    `test_sql_models.py`).
    """
    return SqlWorkflowRepository(session_factory=SessionLocal)


def audit_sink():
    """Where workflow events go. Durable sink first, so a failure in one of the
    mirrors can never cost us the canonical workflow event (see FanOutAuditSink).
    """
    return FanOutAuditSink([
        SqlAuditSink(session_factory=SessionLocal),
        SentAuditMirror(),
        LoggingAuditSink(log),
    ])


def build_orchestrator(authorization) -> Orchestrator:
    """An orchestrator for one caller, over this service's own database.

    Built per request rather than once at startup, because the authorization port
    is bound to the signed-in user's resolved mailbox scope. Everything expensive
    -- the session factory, the registries -- is module-level and shared; this is
    only the small object that holds them together.
    """
    return Orchestrator(
        repository=workflow_repository(),
        definitions=definitions,
        handlers=handlers,
        authorization=authorization,
        audit=audit_sink(),
        routing=build_routing_table(),
    )


def principal_from_claims(claims: dict) -> Principal:
    """A `Principal` from verified token claims only.

    Nothing here reads the request body or query string. The group claim is taken
    as-is; its absence is handled upstream by `deps.mailbox_scope`, which refuses
    the request outright on an Entra group-overage token rather than resolving to
    an empty set that would look like "member of nothing".
    """
    raw = claims.get("groups")
    groups = frozenset(str(g) for g in raw if g) if isinstance(raw, (list, tuple)) else frozenset()
    return Principal(
        user_id=claims.get("sub") or "",
        groups=groups,
        scopes=frozenset((claims.get("scope") or "").split()),
    )
