"""The orchestrator: workflow creation, execution, approval, retry and recovery.

One class, `Orchestrator`, is the shared interface every Project West capability
initiates and executes workflows through. It knows about states, steps, retries,
approvals, routing and audit. It knows nothing about email, voice, referrals,
documents or invoices -- all of that arrives through the ports it is constructed
with (`WorkflowRepository`, `HandlerRegistry`, `AuthorizationPort`,
`ContextProvider`, `AuditSink`), which is what keeps the core reusable and the
capabilities independently testable.

The execution model is deliberately **one unit of work per call**. `advance` runs
at most one step and returns; `run_to_completion` loops over `advance` until the
run reaches a terminal state or has to wait for something outside itself. A single
call that drove a whole workflow to the end could not stop cleanly at an approval
gate, could not be resumed by a different process after a crash, and would hold a
database connection across every network call in the pipeline -- a pattern the mail
agent's own pipeline is carefully built to avoid.

Four guarantees are enforced here rather than left to capabilities:

1. **Nothing consequential runs before approval.** A step declared
   `requires_approval` parks the run in `awaiting_approval` *before* its handler is
   called, and only an `ApprovalRecord` naming a human lets it proceed.
2. **No step runs twice.** Execution is guarded by a database compare-and-set
   (`claim`) so two workers cannot both run a step, and a step already recorded
   `succeeded` is skipped on resume rather than repeated.
3. **Every transition is legal and recorded.** State changes go through the
   lifecycle table and emit a redacted audit event.
4. **An interrupted run is recoverable.** Work is leased rather than locked, so a
   process that dies leaves a run that becomes claimable again instead of one that
   is stuck forever.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from .audit import AuditEvent, AuditSink, LoggingAuditSink
from .context import ContextBundle, ContextProvider, ContextRequest, NullContextProvider
from .definition import DefinitionRegistry, StepSpec, WorkflowDefinition
from .errors import (
    DefinitionError,
    InvalidTransition,
    NotAuthorized,
    OrchestrationError,
    WorkflowNotFound,
)
from .handlers import HandlerRegistry, StepContext, StepOutcome, StepResult
from .principal import SYSTEM_PRINCIPAL, AuthorizationPort, DenyAll, Principal
from .repository import (
    ApprovalRecord,
    StepRecord,
    WorkflowRepository,
    WorkflowRun,
    merge_metadata,
    utcnow,
)
from .routing import RoutingDecision, RoutingFacts, RoutingTable
from .states import WorkflowState, assert_transition, is_terminal

log = logging.getLogger("platform_orchestration.engine")

S = WorkflowState

#: How long a worker's claim on a run is good for. Long enough that a slow step
#: (an LLM call plus a CRM write) finishes inside it; short enough that a killed
#: process does not strand work for an operationally awkward length of time.
DEFAULT_LEASE_SECONDS = 300

#: Exponential backoff, in seconds: base * 2 ** (attempt - 1), capped. Starts at
#: 2s so a transient blip clears on the first retry without a human noticing, and
#: caps at 5 minutes so a persistently failing dependency is retried at a rate
#: that neither hammers it nor forgets about it.
DEFAULT_BACKOFF_BASE = 2.0
DEFAULT_BACKOFF_CAP = 300.0

#: Safety stop on `run_to_completion`. A definition cannot legitimately need more
#: iterations than this, so hitting it means a bug, and looping forever inside a
#: request is a worse way to find out.
MAX_ITERATIONS = 100


class Orchestrator:
    def __init__(
        self,
        *,
        repository: WorkflowRepository,
        definitions: DefinitionRegistry,
        handlers: HandlerRegistry,
        authorization: AuthorizationPort | None = None,
        context_provider: ContextProvider | None = None,
        audit: AuditSink | None = None,
        routing: RoutingTable | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_cap: float = DEFAULT_BACKOFF_CAP,
    ) -> None:
        self._repo = repository
        self._definitions = definitions
        self._handlers = handlers
        # DenyAll, not AllowAll: a host that forgets to wire authorization gets a
        # service that refuses everything. The failure is immediate and safe,
        # rather than an authorization check that silently never happens.
        self._authz = authorization or DenyAll()
        self._context = context_provider or NullContextProvider()
        self._audit = audit or LoggingAuditSink()
        self._routing = routing
        self._lease = timedelta(seconds=lease_seconds)
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap

    # ── audit ────────────────────────────────────────────────────────────────

    async def _emit(self, event: AuditEvent) -> None:
        """Every event leaves here redacted. There is no path that skips this."""
        await self._audit.record(event.redacted())

    @staticmethod
    def _actor_kind(principal: Principal) -> str:
        if principal.service:
            return "service"
        return "user" if principal.user_id else "system"

    async def _transition(
        self,
        run: WorkflowRun,
        to_state: WorkflowState,
        *,
        principal: Principal,
        action: str,
        step: str | None = None,
        detail: Mapping[str, Any] | None = None,
        sources: tuple[str, ...] = (),
    ) -> WorkflowRun:
        """The only way a run changes state. Validates, applies, persists, audits."""
        frm = run.state
        assert_transition(frm, to_state)
        run.state = to_state
        if is_terminal(to_state):
            run.finished_at = utcnow()
            # A terminal run holds no lease: leaving one behind would make a
            # finished run look briefly like work in progress to a recovery sweep.
            run.lease_expires_at = None
        run.touch()
        await self._repo.save_run(run)
        await self._emit(AuditEvent(
            run_id=run.id,
            action=action,
            capability=run.capability,
            subject_ref=run.subject_ref,
            step=step,
            from_state=frm.value,
            to_state=to_state.value,
            actor_user_id=principal.user_id or None,
            actor_kind=self._actor_kind(principal),
            attempt=run.attempts or None,
            sources=sources,
            detail=dict(detail or {}),
        ))
        return run

    # ── creation ─────────────────────────────────────────────────────────────

    async def start(
        self,
        *,
        definition: str,
        subject_ref: str,
        principal: Principal,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        facts: RoutingFacts | None = None,
        queue: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> WorkflowRun:
        """Create a workflow run, or return the one that already exists for this key.

        `idempotency_key` is what makes this safe to call from a cron tick, a retry
        or a double-clicked button: a second call with the same key over the same
        definition returns the original run and records that it did, rather than
        starting a parallel workflow over the same record. A caller that passes no
        key is asking for a new run every time, which is occasionally what is
        wanted and never the default for automated callers.
        """
        spec = self._definitions.get(definition)

        if not await self._authz.can_start(
            principal, capability=spec.capability, subject_ref=subject_ref
        ):
            raise NotAuthorized(
                "not authorized to start %s workflows over %s"
                % (spec.capability, subject_ref)
            )

        if idempotency_key:
            existing = await self._repo.find_by_idempotency_key(
                definition=definition, idempotency_key=idempotency_key
            )
            if existing is not None:
                # Recorded, not silent. "Why is there only one run when the cron
                # fired twice?" has to be answerable, and a suppressed duplicate
                # that leaves no trace is indistinguishable from a lost message.
                await self._emit(AuditEvent(
                    run_id=existing.id,
                    action="duplicate_suppressed",
                    capability=existing.capability,
                    subject_ref=existing.subject_ref,
                    actor_user_id=principal.user_id or None,
                    actor_kind=self._actor_kind(principal),
                    detail={
                        "definition": definition,
                        "idempotency_key": idempotency_key,
                        "existing_state": existing.state.value,
                    },
                ))
                return existing

        decision: RoutingDecision | None = None
        if facts is not None and self._routing is not None:
            decision = self._routing.route(facts)

        run = WorkflowRun(
            definition=spec.name,
            capability=spec.capability,
            subject_ref=subject_ref,
            version=spec.version,
            state=S.CREATED,
            idempotency_key=idempotency_key,
            started_by=principal.user_id or None,
            queue=queue or (decision.target.queue if decision else ""),
            routing_rule=decision.rule_name if decision else "",
            payload=dict(payload or {}),
            metadata=dict(metadata or {}),
        )
        if decision is not None:
            run.metadata = merge_metadata(run.metadata, {
                "routing_reason": decision.reason,
                "routing_matched": decision.matched,
                **dict(decision.target.metadata),
            })

        run = await self._repo.create_run(run)
        await self._emit(AuditEvent(
            run_id=run.id,
            action="created",
            capability=run.capability,
            subject_ref=run.subject_ref,
            to_state=run.state.value,
            actor_user_id=principal.user_id or None,
            actor_kind=self._actor_kind(principal),
            detail={
                "definition": run.definition,
                "version": run.version,
                "queue": run.queue,
                "routing_rule": run.routing_rule,
                "steps": list(spec.step_names),
            },
        ))
        return run

    # ── reads ────────────────────────────────────────────────────────────────

    async def get(self, run_id: str, *, principal: Principal) -> WorkflowRun:
        """The run, if this principal may know it exists.

        Raises `WorkflowNotFound` for both "no such run" and "not yours", because
        distinguishing them tells an unauthorised caller that the record exists.
        """
        run = await self._repo.get_run(run_id)
        if run is None:
            raise WorkflowNotFound(run_id)
        if not await self._authz.can_view(
            principal, capability=run.capability, subject_ref=run.subject_ref
        ):
            raise WorkflowNotFound(run_id)
        return run

    async def steps(self, run_id: str, *, principal: Principal) -> Sequence[StepRecord]:
        await self.get(run_id, principal=principal)   # authorization first
        return await self._repo.steps_for(run_id)

    async def list_runs(
        self,
        *,
        principal: Principal,
        capability: str | None = None,
        states: Sequence[WorkflowState] | None = None,
        queue: str | None = None,
        subject_refs: Sequence[str] | None = None,
        limit: int = 100,
    ) -> Sequence[WorkflowRun]:
        """Runs this principal may see.

        `subject_refs` is the authorised set and is applied *in the query*. The
        host is expected to pass it; when it does not, every row is filtered
        through `can_view` instead, which is correct but does more work. Passing
        an empty sequence means "entitled to nothing" and returns nothing.
        """
        rows = await self._repo.list_runs(
            capability=capability, states=states, queue=queue,
            subject_refs=subject_refs, limit=limit,
        )
        if subject_refs is not None:
            return rows
        # One port call per row. Correct, but a query per listed run, which is why
        # a host that already knows the authorised set should pass `subject_refs`
        # and have the database do the filtering instead.
        permitted: list[WorkflowRun] = []
        for r in rows:
            if await self._authz.can_view(
                principal, capability=r.capability, subject_ref=r.subject_ref
            ):
                permitted.append(r)
        return tuple(permitted)

    # ── execution ────────────────────────────────────────────────────────────

    async def advance(self, run_id: str, *, principal: Principal) -> WorkflowRun:
        """Perform at most one step of this run, and return it.

        Returns the run unchanged, without error, when there is nothing to do:
        it is terminal, it is waiting for a human, or its retry is not yet due.
        "Nothing to do" is a normal answer here -- a sweep calls this across many
        runs and must not have to pre-filter them.
        """
        run = await self.get(run_id, principal=principal)

        if is_terminal(run.state):
            return run
        if run.state is S.AWAITING_APPROVAL:
            return run
        if run.state is S.RETRY_SCHEDULED:
            if run.next_attempt_at is not None and run.next_attempt_at > utcnow():
                return run

        spec = self._definitions.get(run.definition)

        # A definition whose shape changed under an in-flight run. Executing the
        # new step list against a run that began on the old one would run the
        # wrong step, or re-run one already done, so the run parks for a human
        # instead. Checked before the claim so a version mismatch does not consume
        # a lease on every sweep.
        if run.version != spec.version:
            run.last_error = (
                "definition %r is at version %d but this run started on version %d"
                % (spec.name, spec.version, run.version)
            )
            return await self._transition(
                run, S.EXCEPTION, principal=principal, action="version_mismatch",
                detail={"definition_version": spec.version, "run_version": run.version},
            )

        # The compare-and-set. Everything above this line is a read; everything
        # below runs exactly once across all workers, for this run, for this step.
        assert_transition(run.state, S.RUNNING)
        claimed = await self._repo.claim(
            run_id,
            expected_state=run.state,
            to_state=S.RUNNING,
            lease_until=utcnow() + self._lease,
        )
        if claimed is None:
            # Somebody else has it, or it moved on between our read and now.
            # Leaving it alone is the correct response, and is what stops the
            # same outbound effect happening twice.
            log.debug("run %s not claimable (state=%s)", run_id, run.state.value)
            return run

        try:
            return await self._execute_current_step(claimed, spec, principal)
        except Exception:
            # Something failed *outside* a step handler -- an unregistered handler
            # key, a context provider raising, a storage error. A handler's own
            # exception never reaches here; `_run_handler` catches those and turns
            # them into a retry.
            #
            # The run must not be left in `running`: the lease is dropped in the
            # `finally` below, and a `running` run with no lease is invisible to
            # `recover_stale` (which looks for lapsed leases), so it would sit
            # there forever with nothing to pick it up. Parking it in `exception`
            # makes it visible and still resumable once the cause is fixed.
            log.exception("advancing run %s failed outside its handler", claimed.id)
            claimed.last_error = "orchestration error while advancing this step"
            try:
                return await self._transition(
                    claimed, S.EXCEPTION, principal=principal,
                    action="orchestration_error",
                    detail={"reason": "failed outside the step handler"},
                )
            except InvalidTransition:
                # Already moved on; nothing useful left to do but let it surface.
                raise
        finally:
            # The lease is dropped whatever happened. A run left leased after its
            # worker returned would be invisible to the next sweep until expiry,
            # which turns a fast retry into a five-minute one.
            await self._repo.release(claimed.id)

    async def _execute_current_step(
        self, run: WorkflowRun, spec: WorkflowDefinition, principal: Principal
    ) -> WorkflowRun:
        step_spec = spec.step(run.step_index)
        if step_spec is None:
            # Ran off the end of the step list: every step is done.
            return await self._finish(run, principal)

        record = await self._repo.get_step(run.id, step_spec.name)

        # Already done. This is the duplicate-effect guard on the resume path: a
        # run interrupted after its CRM write but before its state was persisted
        # comes back here, finds the step succeeded, and moves on instead of
        # filing the referral a second time.
        if record is not None and record.succeeded:
            await self._emit(AuditEvent(
                run_id=run.id, action="step_already_done",
                capability=run.capability, subject_ref=run.subject_ref,
                step=step_spec.name, actor_kind=self._actor_kind(principal),
                detail={"reason": "step already recorded as succeeded; not re-run"},
            ))
            return await self._advance_index(run, spec, principal)

        if step_spec.requires_approval:
            gate = await self._approval_gate(run, step_spec, principal)
            if gate is not None:
                return gate

        return await self._run_handler(run, spec, step_spec, record, principal)

    async def _approval_gate(
        self, run: WorkflowRun, step_spec: StepSpec, principal: Principal
    ) -> WorkflowRun | None:
        """Park the run unless a decision already exists. None means "proceed".

        This is the control that makes "no consequential action without approval"
        true in the engine rather than in each capability. The handler for a gated
        step is not called at all until an `ApprovalRecord` exists for it.
        """
        approval = await self._repo.approval_for(run.id, step_spec.name)

        if approval is None:
            run.attempts = 0
            return await self._transition(
                run, S.AWAITING_APPROVAL, principal=principal,
                action="approval_requested", step=step_spec.name,
                detail={
                    "consequential": step_spec.consequential,
                    "reason": (
                        "this step requires a standing policy or a human decision "
                        "before it runs"
                        if step_spec.policy_approval
                        else "this step requires a human decision before it runs"
                    ),
                },
            )

        if approval.decision != "approved":
            run.last_error = "rejected at step %r" % step_spec.name
            return await self._transition(
                run, S.REJECTED, principal=principal, action="rejected",
                step=step_spec.name,
                detail={"decided_by": approval.decided_by, "note": approval.note},
            )

        return None

    async def _run_handler(
        self,
        run: WorkflowRun,
        spec: WorkflowDefinition,
        step_spec: StepSpec,
        record: StepRecord | None,
        principal: Principal,
    ) -> WorkflowRun:
        handler = self._handlers.get(step_spec.handler)

        context = await self._gather_context(run, step_spec, principal)
        sources = tuple(ref.key() for ref in context.references())

        run.attempts += 1
        attempt = run.attempts

        record = record or StepRecord(run_id=run.id, name=step_spec.name)
        record.state = S.RUNNING
        record.attempts = attempt
        record.started_at = record.started_at or utcnow()
        await self._repo.save_step(record)
        await self._repo.save_run(run)

        await self._emit(AuditEvent(
            run_id=run.id, action="step_started", capability=run.capability,
            subject_ref=run.subject_ref, step=step_spec.name,
            actor_user_id=principal.user_id or None,
            actor_kind=self._actor_kind(principal),
            attempt=attempt, sources=sources,
            detail={"handler": step_spec.handler, "consequential": step_spec.consequential},
        ))

        ctx = StepContext(
            run_id=run.id,
            capability=run.capability,
            subject_ref=run.subject_ref,
            step=step_spec,
            attempt=attempt,
            principal=principal,
            context=context,
            outputs=await self._collected_outputs(run.id),
            payload=dict(run.payload),
            logger=logging.getLogger(
                "platform_orchestration.step.%s.%s" % (run.capability, step_spec.name)
            ),
        )

        try:
            result = await handler(ctx)
        except Exception as exc:
            # An unhandled exception is treated as *retryable*. A handler that
            # knows its failure is permanent says so with `StepResult.fail`; an
            # escaped exception is more often a timeout, a dropped connection or a
            # restart mid-deploy, and abandoning the run on the first one would
            # make the system fragile in exactly the conditions retry exists for.
            log.exception("step %s/%s raised", run.id, step_spec.name)
            result = StepResult.retry("unhandled error: %s" % exc)

        return await self._apply_result(run, spec, step_spec, record, result, principal, sources)

    async def _apply_result(
        self,
        run: WorkflowRun,
        spec: WorkflowDefinition,
        step_spec: StepSpec,
        record: StepRecord,
        result: StepResult,
        principal: Principal,
        sources: tuple[str, ...],
    ) -> WorkflowRun:
        if result.outcome is StepOutcome.OK:
            record.state = S.SUCCEEDED
            record.output = dict(result.output)
            record.error = None
            record.finished_at = utcnow()
            await self._repo.save_step(record)
            await self._emit(AuditEvent(
                run_id=run.id, action="step_succeeded", capability=run.capability,
                subject_ref=run.subject_ref, step=step_spec.name,
                actor_user_id=principal.user_id or None,
                actor_kind=self._actor_kind(principal),
                attempt=run.attempts, sources=sources,
                detail={"output": dict(result.output)},
            ))
            return await self._advance_index(run, spec, principal)

        if result.outcome is StepOutcome.NEEDS_REVIEW:
            record.state = S.EXCEPTION
            record.error = result.error
            await self._repo.save_step(record)
            run.last_error = result.error
            return await self._transition(
                run, S.EXCEPTION, principal=principal, action="needs_review",
                step=step_spec.name,
                detail={"error": result.error, "output": dict(result.output)},
            )

        if result.outcome is StepOutcome.FAIL:
            # Permanent. Retry budget is irrelevant -- repeating a request the
            # other side has already rejected on its merits only wastes time and
            # muddies the audit trail.
            record.state = S.FAILED
            record.error = result.error
            record.finished_at = utcnow()
            await self._repo.save_step(record)
            run.last_error = result.error
            return await self._transition(
                run, S.FAILED, principal=principal, action="failed",
                step=step_spec.name,
                detail={"error": result.error, "permanent": True},
            )

        # RETRY
        if run.attempts >= step_spec.max_attempts:
            record.state = S.FAILED
            record.error = result.error
            record.finished_at = utcnow()
            await self._repo.save_step(record)
            run.last_error = (
                "%s (gave up after %d attempt(s))" % (result.error, run.attempts)
            )
            return await self._transition(
                run, S.FAILED, principal=principal, action="failed",
                step=step_spec.name,
                detail={
                    "error": result.error,
                    "attempts": run.attempts,
                    "max_attempts": step_spec.max_attempts,
                    "exhausted": True,
                },
            )

        delay = (
            result.retry_after if result.retry_after is not None
            else self._backoff(run.attempts)
        )
        record.state = S.RETRY_SCHEDULED
        record.error = result.error
        await self._repo.save_step(record)
        run.next_attempt_at = utcnow() + timedelta(seconds=delay)
        run.last_error = result.error
        return await self._transition(
            run, S.RETRY_SCHEDULED, principal=principal, action="retry_scheduled",
            step=step_spec.name,
            detail={
                "error": result.error,
                "attempt": run.attempts,
                "max_attempts": step_spec.max_attempts,
                "retry_in_seconds": round(delay, 3),
            },
        )

    def _backoff(self, attempt: int) -> float:
        return min(self._backoff_base * (2 ** max(0, attempt - 1)), self._backoff_cap)

    async def _advance_index(
        self, run: WorkflowRun, spec: WorkflowDefinition, principal: Principal
    ) -> WorkflowRun:
        """Move to the next step, or finish. Resets the per-step attempt counter."""
        run.step_index += 1
        run.attempts = 0
        run.next_attempt_at = None
        run.last_error = None

        if run.step_index >= len(spec.steps):
            return await self._finish(run, principal)

        await self._repo.save_run(run)
        return run

    async def _finish(self, run: WorkflowRun, principal: Principal) -> WorkflowRun:
        return await self._transition(
            run, S.SUCCEEDED, principal=principal, action="succeeded",
            detail={"steps_completed": run.step_index},
        )

    async def _gather_context(
        self, run: WorkflowRun, step_spec: StepSpec, principal: Principal
    ) -> ContextBundle:
        """Authorised context for this step, or an empty bundle.

        The provider is only asked for what the step declared it needs, and only
        on behalf of the principal the run is executing for -- so a workflow can
        never see more of the CRM or SharePoint than the person it is acting for.
        Provenance is folded into the run's metadata so a completed run can still
        say where its inputs came from.
        """
        if not step_spec.context_slots:
            return ContextBundle()

        bundle = await self._context.fetch(ContextRequest(
            principal=principal,
            capability=run.capability,
            subject_ref=run.subject_ref,
            slots=step_spec.context_slots,
        ))

        refs = [ref.key() for ref in bundle.references()]
        if refs or bundle.withheld:
            known = list(run.metadata.get("sources") or [])
            for ref in refs:
                if ref not in known:
                    known.append(ref)
            run.metadata = merge_metadata(run.metadata, {"sources": known})
            if bundle.withheld:
                run.metadata = merge_metadata(run.metadata, {
                    "context_withheld": sorted(set(bundle.withheld)),
                })
        return bundle

    async def _collected_outputs(self, run_id: str) -> dict[str, dict[str, Any]]:
        records = await self._repo.steps_for(run_id)
        return {r.name: dict(r.output) for r in records if r.succeeded}

    async def run_to_completion(
        self, run_id: str, *, principal: Principal, max_iterations: int = MAX_ITERATIONS
    ) -> WorkflowRun:
        """Advance repeatedly until the run finishes or has to wait.

        Stops on a terminal state, on an approval gate, on a scheduled retry whose
        time has not come, and whenever a call makes no progress -- the last of
        which is what keeps a contended run from spinning here while another
        worker holds it.
        """
        run = await self.advance(run_id, principal=principal)
        for _ in range(max_iterations):
            if is_terminal(run.state) or run.state in (S.AWAITING_APPROVAL, S.EXCEPTION):
                return run
            if run.state is S.RETRY_SCHEDULED:
                if run.next_attempt_at is not None and run.next_attempt_at > utcnow():
                    return run
            before = (run.state, run.step_index, run.attempts)
            run = await self.advance(run_id, principal=principal)
            if (run.state, run.step_index, run.attempts) == before:
                return run
        log.warning("run %s hit the iteration ceiling (%d)", run_id, max_iterations)
        return run

    # ── human approval ───────────────────────────────────────────────────────

    async def submit_approval(
        self,
        run_id: str,
        *,
        principal: Principal,
        approved: bool,
        note: str | None = None,
    ) -> WorkflowRun:
        """Record a human decision on the step this run is waiting at.

        Refuses a service principal outright. An automated caller approving its
        own workflow would make the gate ceremonial, and the whole point of the
        state is that a *person* took responsibility for what happens next.
        """
        run = await self.get(run_id, principal=principal)

        if run.state is not S.AWAITING_APPROVAL:
            raise InvalidTransition(run.state.value, "approved" if approved else "rejected")

        if not await self._authz.can_approve(
            principal, capability=run.capability, subject_ref=run.subject_ref
        ):
            raise NotAuthorized("not authorized to approve %s" % run_id)

        if not principal.is_human:
            raise NotAuthorized("approval requires a human principal")

        spec = self._definitions.get(run.definition)
        step_spec = spec.step(run.step_index)
        step_name = step_spec.name if step_spec else ""

        await self._repo.record_approval(ApprovalRecord(
            run_id=run.id,
            step=step_name,
            decision="approved" if approved else "rejected",
            decided_by=principal.user_id,
            note=note,
        ))

        if not approved:
            run.last_error = "rejected at step %r" % step_name
            return await self._transition(
                run, S.REJECTED, principal=principal, action="rejected", step=step_name,
                detail={"note": note},
            )

        return await self._transition(
            run, S.APPROVED, principal=principal, action="approved", step=step_name,
            detail={"note": note, "consequential": bool(step_spec and step_spec.consequential)},
        )

    async def submit_policy_approval(
        self,
        run_id: str,
        *,
        principal: Principal,
        policy: str,
        note: str | None = None,
    ) -> WorkflowRun:
        """Record a *standing policy* as the authority for the gated step.

        The mirror image of `submit_approval`, and deliberately not a relaxation
        of it:

          * `submit_approval` takes a human and refuses a service principal.
          * this takes a service principal and refuses a human -- a person's
            decision is their own and must be recorded as theirs, never laundered
            through a policy name.

        It only works on a step whose definition opted in with
        `policy_approval=True`. Every other gated step is unreachable this way, so
        adding this method cannot have loosened a gate that already existed.

        What it is for: a capability an administrator has deliberately configured
        to act autonomously. The alternative to recording that configuration as
        the approving authority is not a stricter system -- it is the same send
        happening with no approval record at all, which is the gap this closes.
        """
        if not policy:
            raise OrchestrationError(
                "a policy approval must name the policy that authorised it"
            )

        run = await self.get(run_id, principal=principal)

        if run.state is not S.AWAITING_APPROVAL:
            raise InvalidTransition(run.state.value, "approved")

        if principal.is_human:
            raise NotAuthorized(
                "a policy approval is the system acting on a standing "
                "configuration; a person approves through submit_approval"
            )
        if not principal.service:
            raise NotAuthorized("a policy approval requires a service principal")

        spec = self._definitions.get(run.definition)
        step_spec = spec.step(run.step_index)
        step_name = step_spec.name if step_spec else ""

        if step_spec is None or not step_spec.policy_approval:
            raise NotAuthorized(
                "step %r does not accept a policy approval; it requires a human"
                % step_name
            )

        # The host still decides whether this principal may act on this subject at
        # all. Opting the step in says a policy *may* approve it, not that any
        # caller may.
        if not await self._authz.can_start(
            principal, capability=run.capability, subject_ref=run.subject_ref
        ):
            raise NotAuthorized("not authorized to act on %s" % run_id)

        await self._repo.record_approval(ApprovalRecord(
            run_id=run.id,
            step=step_name,
            decision="approved",
            # Namespaced so an audit reader can never mistake a standing policy
            # for a person who pressed Approve.
            decided_by="policy:%s" % policy,
            note=note,
        ))

        return await self._transition(
            run, S.APPROVED, principal=principal, action="approved_by_policy",
            step=step_name,
            detail={
                "policy": policy,
                "note": note,
                "consequential": bool(step_spec.consequential),
            },
        )

    async def policy_approve_and_continue(
        self,
        run_id: str,
        *,
        principal: Principal,
        policy: str,
        note: str | None = None,
    ) -> WorkflowRun:
        """`submit_policy_approval` then carry on, as `approve_and_continue` does."""
        run = await self.submit_policy_approval(
            run_id, principal=principal, policy=policy, note=note
        )
        if run.state is not S.APPROVED:
            return run
        return await self.run_to_completion(run_id, principal=principal)

    async def approve_and_continue(
        self,
        run_id: str,
        *,
        principal: Principal,
        approved: bool,
        note: str | None = None,
    ) -> WorkflowRun:
        """Record the decision and, on approval, carry on executing.

        The convenience an API route wants: one call turns "the reviewer pressed
        Approve" into "the reply has been sent", while still writing the approval
        record first so the decision is durable before anything acts on it.
        """
        run = await self.submit_approval(
            run_id, principal=principal, approved=approved, note=note
        )
        if run.state is not S.APPROVED:
            return run
        return await self.run_to_completion(run_id, principal=principal)

    # ── recovery ─────────────────────────────────────────────────────────────

    async def cancel(
        self, run_id: str, *, principal: Principal, reason: str = ""
    ) -> WorkflowRun:
        run = await self.get(run_id, principal=principal)
        if not await self._authz.can_approve(
            principal, capability=run.capability, subject_ref=run.subject_ref
        ):
            raise NotAuthorized("not authorized to cancel %s" % run_id)
        run.last_error = reason or "cancelled"
        return await self._transition(
            run, S.CANCELLED, principal=principal, action="cancelled",
            detail={"reason": reason},
        )

    async def resume_due(
        self, *, principal: Principal = SYSTEM_PRINCIPAL, limit: int = 50
    ) -> list[WorkflowRun]:
        """Advance every run whose scheduled retry has come due.

        Called from the host's existing scheduler tick. Each run is advanced
        independently and a failure on one does not stop the sweep -- a single
        poison run must not prevent every other workflow from recovering.
        """
        due = await self._repo.due_runs(limit=limit)
        resumed: list[WorkflowRun] = []
        for run in due:
            try:
                resumed.append(await self.run_to_completion(run.id, principal=principal))
            except Exception:
                log.exception("resuming run %s failed", run.id)
        return resumed

    async def resume_policy_gated(
        self,
        *,
        policy: str,
        principal: Principal = SYSTEM_PRINCIPAL,
        definition: str | None = None,
        limit: int = 50,
    ) -> list[WorkflowRun]:
        """Release runs parked at a *policy* gate that nothing has approved yet.

        The third sweep, and the one the other two cannot cover. `resume_due`
        looks at `retry_scheduled` and `recover_stale` at `running` with a lapsed
        lease; a run sitting in `awaiting_approval` is deliberately outside both,
        because a run waiting for a person must wait indefinitely.

        A run waiting for a *policy* is different: the authority already exists,
        so the wait is not meaningful and nothing else will end it. That happens
        whenever a process dies between starting such a run and approving it --
        the run is then stranded forever, since no sweep looks at its state and
        no person is expected to act on it.

        Only steps that opted in with `policy_approval=True` are touched, so a
        run awaiting a human is never swept up by this. `definition` narrows it
        further, which is how a host with both human-gated and policy-gated
        workflows keeps this sweep to the latter.
        """
        parked = await self._repo.list_runs(
            states=[S.AWAITING_APPROVAL], limit=limit,
        )
        released: list[WorkflowRun] = []
        for run in parked:
            if definition is not None and run.definition != definition:
                continue
            try:
                spec = self._definitions.get(run.definition)
            except DefinitionError:
                # A run of a definition this process no longer knows. `advance`
                # already parks those in `exception`; a sweep is not the place to
                # decide their fate.
                continue
            step_spec = spec.step(run.step_index)
            if step_spec is None or not step_spec.policy_approval:
                continue
            try:
                released.append(await self.policy_approve_and_continue(
                    run.id, principal=principal, policy=policy,
                    note="released by the standing policy sweep after an "
                         "interrupted run",
                ))
            except Exception:
                log.exception("releasing policy-gated run %s failed", run.id)
        return released

    async def recover_stale(
        self, *, principal: Principal = SYSTEM_PRINCIPAL, limit: int = 50
    ) -> list[WorkflowRun]:
        """Reclaim runs abandoned mid-step by a worker that died.

        A lapsed lease on a `running` run means no process is working on it any
        more. The run is put back to `retry_scheduled` so the ordinary retry path
        picks it up, which keeps one code path for "try this step again" instead
        of a second, less-tested one for crash recovery. The attempt already spent
        is not refunded: a step that reliably kills its worker must exhaust its
        budget and surface, not retry forever.
        """
        stale = await self._repo.stale_runs(limit=limit)
        recovered: list[WorkflowRun] = []
        for run in stale:
            spec = self._definitions.get(run.definition)
            step_spec = spec.step(run.step_index)
            step_name = step_spec.name if step_spec else None
            run.next_attempt_at = utcnow()
            run.lease_expires_at = None
            run.last_error = "worker lease expired mid-step; rescheduled"
            try:
                recovered.append(await self._transition(
                    run, S.RETRY_SCHEDULED, principal=principal,
                    action="recovered_stale", step=step_name,
                    detail={"attempts_spent": run.attempts},
                ))
            except InvalidTransition:
                # It moved on between the sweep's read and now. Nothing to do.
                log.debug("stale run %s already moved to %s", run.id, run.state.value)
        return recovered
