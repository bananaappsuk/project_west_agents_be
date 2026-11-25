"""The voice agent's wiring into the shared orchestration foundation.

This module is the whole of the voice agent's coupling to
`platform_orchestration`: the authorization adapter, the principal mapping and
the storage/audit ports. The engine imports nothing from here, and nothing here
reimplements anything the engine provides -- which is what lets the mail agent
and this service share one library and one set of workflow tables without
either knowing about the other.

Phase 3 scope
-------------

Ports only. No handler, no workflow definition and no endpoint is registered
here yet: those arrive with `voice.intake` in Phase 4. What this file
establishes is the authorization boundary everything later runs behind, which is
the part that has to be right before anything can start a run.

Two boundaries it is responsible for
------------------------------------

**Capability isolation.** The voice service and the mail agent share one
Postgres database and therefore one `workflow_runs` table, separated only by the
`capability` column. `Orchestrator.list_runs(capability=...)` is optional, so a
caller that forgets it would see mail runs. Rather than rely on every call site
remembering, `RecordingAuthorizationPort` refuses any capability that is not
`voice` outright -- the engine consults the port before it returns a run, so a
mail run cannot come back through a voice orchestrator whatever the caller
passes.

**Safeguarding restriction, fail closed.** A recording whose route is
`safeguarding_restricted` is a safeguarding disclosure. Orchestration must not
become a new way to read one, so `can_view` refuses it for every human
principal. That is deliberately stricter than the four existing recording read
endpoints, which this change does not touch: the rule here is "no new access",
not "retrospectively narrow the old access", and narrowing those endpoints is a
separate decision that has not been taken.

Note on the wider safeguarding model: the mail agent holds a real one --
`safeguarding_clearance`, a per-user grant with its own audit history, where
"an application Superadmin does not receive it implicitly" because "the
administrative role that configures the system is not the clinical role that
handles disclosures" (`mail_agent/app/mailbox_scope.py`). The voice service has
no equivalent, and inventing one here would be a policy decision rather than an
engineering one. Until that decision is taken, `can_view` denies, which is the
reversible direction.
"""

from __future__ import annotations

import logging

from platform_orchestration import (
    AuditEvent,  # noqa: F401  (re-exported for the handlers that arrive in Phase 4)
    FanOutAuditSink,
    LoggingAuditSink,
    Orchestrator,
    Principal,
)
from platform_orchestration.sql import SqlAuditSink, SqlWorkflowRepository
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import SessionLocal
from .models import Recording

log = logging.getLogger("voice_agent.orchestration")

#: This service's capability name on every workflow run it starts. The engine
#: stores it on the row and the port below refuses anything else, which is what
#: keeps voice runs and mail runs apart in a table they share.
CAPABILITY = "voice"

#: The route the agent factory's taxonomy assigns to a safeguarding disclosure.
#: The canonical owner of this vocabulary is
#: `services/agent_factory/app/taxonomy.py`; this service receives it as the
#: `route` field on an analysis and stores it on the recording. Duplicated as a
#: literal rather than imported because the factory is a separate service with
#: no shared package -- `test_voice_authorization.py` asserts this constant
#: still matches the value `app/api.py` filters on, so the two cannot drift
#: apart silently.
RESTRICTED_ROUTE = "safeguarding_restricted"

#: The scopes this service already issues. Unchanged by this work: no new scope
#: is introduced, and `{app}:admin` keeps the bypass meaning it has in
#: `app/deps.py` for the endpoints that already honour it.
READ_SCOPE = "recordings.read"
WRITE_SCOPE = "recordings.write"


def _scoped(app_key: str, suffix: str) -> str:
    return "%s:%s" % (app_key, suffix)


async def is_restricted(session: AsyncSession, subject_ref: str) -> bool:
    """Whether this recording is a safeguarding disclosure.

    Fails closed in both unknown cases, and they are different unknowns:

      * the recording does not exist -- nothing legitimate is being asked for;
      * the lookup raised -- the answer is not known, and "not known" must not
        read as "not restricted".

    A `True` here removes access, so an error that defaulted to `False` would
    silently open the one thing this predicate exists to close.
    """
    try:
        route = await session.scalar(
            select(Recording.route).where(Recording.id == subject_ref)
        )
    except Exception:  # noqa: BLE001 -- see the docstring: unknown means restricted
        log.exception("restricted-route lookup failed for %s; treating as restricted",
                      subject_ref)
        return True
    if route is None:
        return True
    return route == RESTRICTED_ROUTE


class RecordingAuthorizationPort:
    """`AuthorizationPort` over this service's recordings.

    Every method is async because the restriction check is a database read, and
    every method fails closed: an unanswerable question returns False, which the
    engine treats as a refusal it does not retry.
    """

    def __init__(self, app_key: str, *, session_factory=None) -> None:
        self._app_key = app_key
        self._sessions = session_factory or SessionLocal

    # ── the grants, named once ──────────────────────────────────────────────
    def _has(self, principal: Principal, suffix: str) -> bool:
        """Scope check, with the same admin bypass `app/deps.py` already applies.

        Mirrored rather than imported so this port can be tested without a
        FastAPI dependency, and kept identical so orchestration cannot be a
        second, differently-shaped answer to "may this user act here".
        """
        granted = set(principal.scopes)
        return (
            _scoped(self._app_key, "admin") in granted
            or _scoped(self._app_key, suffix) in granted
        )

    def _wrong_capability(self, capability: str) -> bool:
        if capability != CAPABILITY:
            log.warning(
                "refusing a %r workflow through the voice port", capability
            )
            return True
        return False

    # ── the port ────────────────────────────────────────────────────────────
    async def can_start(
        self, principal: Principal, *, capability: str, subject_ref: str
    ) -> bool:
        """May this principal start a voice workflow over this recording?

        The pipeline runs as `SYSTEM_PRINCIPAL` and must be able to process
        every recording, restricted ones included -- a safeguarding call that
        was never analysed is worse than one that was. So the system principal
        is granted here explicitly, which the library's own docstring says a
        host must do rather than receive by accident.

        A human needs the write scope, which is exactly what
        `POST /recordings/{id}/retry-analysis` already requires. Restriction is
        deliberately *not* consulted: that endpoint works on a restricted
        recording today, and refusing it here would narrow existing access
        rather than avoid widening it.
        """
        if self._wrong_capability(capability):
            return False
        if principal.service:
            return True
        if not principal.is_human:
            return False
        return self._has(principal, WRITE_SCOPE)

    async def can_view(
        self, principal: Principal, *, capability: str, subject_ref: str
    ) -> bool:
        """May this principal know this run exists and read its context?

        This is the fail-closed one. Orchestration must not become a new path to
        a safeguarding disclosure, so a restricted recording is refused to every
        human principal regardless of scope -- including `{app}:admin`, because
        the administrative role is not the clinical one.

        Nothing human-facing calls this during the intake work: there is no
        workflow review endpoint, by decision. It is written strictly now so
        that adding one later is a deliberate act with a test to change, rather
        than something that quietly inherits read access.
        """
        if self._wrong_capability(capability):
            return False
        if principal.service:
            return True
        if not principal.is_human:
            return False
        if not self._has(principal, READ_SCOPE):
            return False

        async with self._sessions() as session:
            if await is_restricted(session, subject_ref):
                log.info(
                    "withholding restricted recording %s from %s",
                    subject_ref,
                    principal.user_id,
                )
                return False
        return True

    async def can_approve(
        self, principal: Principal, *, capability: str, subject_ref: str
    ) -> bool:
        """May this principal make a human approval decision on this run?

        No voice workflow has an approval step yet -- the CRM is the approval
        authority for intake, and the reply gate is Phase 6 and AC-8, not this
        work. So this exists to be correct rather than to be used, and it is
        written to refuse the two things that would be wrong if a step ever did
        wait for approval:

          * a service principal, because "the system approved it" is not human
            review -- the library's own note on `Principal.service`;
          * a restricted recording, on the same footing as `can_view`: nobody
            approves what they may not read.

        Phase 6 is expected to introduce a distinct approve grant rather than
        reuse the write scope, as the mail agent did (DECISIONS.md D2, D9). It
        is the write scope for now because no approval path exists to gate.
        """
        if self._wrong_capability(capability):
            return False
        if principal.service:
            return False
        if not principal.is_human:
            return False
        if not self._has(principal, WRITE_SCOPE):
            return False

        async with self._sessions() as session:
            if await is_restricted(session, subject_ref):
                return False
        return True


def principal_from_claims(claims: dict) -> Principal:
    """A verified token's claims, as the orchestration layer sees them.

    `sub` only: no fallback to an email or a name, because a principal whose id
    can come from two places is a principal two tokens can share. A token
    without `sub` produces a principal that `is_human` rejects.
    """
    groups = claims.get("groups") or []
    return Principal(
        user_id=str(claims.get("sub") or ""),
        scopes=frozenset((claims.get("scope") or "").split()),
        groups=frozenset(str(g) for g in groups if g),
        service=False,
    )


def workflow_repository() -> SqlWorkflowRepository:
    """This service's workflow store.

    A function rather than an expression inside `build_orchestrator`, so the
    storage port has one named seam a test can substitute
    `InMemoryWorkflowRepository` at -- the service's suite has no database.

    The rows land in the four `workflow_*` tables, which already exist in this
    database because the mail agent's `create_all` created them and the two
    services share one database. Nothing here creates or alters a table.
    """
    return SqlWorkflowRepository(session_factory=SessionLocal)


def audit_sink() -> FanOutAuditSink:
    """Where workflow events go. Durable sink first.

    `FanOutAuditSink` is ordered so a failure in the log sink cannot cost the
    canonical row.
    """
    return FanOutAuditSink([
        SqlAuditSink(session_factory=SessionLocal),
        LoggingAuditSink(log),
    ])


# ── the workflow ────────────────────────────────────────────────────────────
#
# One step, not three, and the reason is a hard constraint rather than a
# preference.
#
# `crm_sync.after_call_analysis` reads thirteen keys off the analysis dict:
# case_ref, child_lives_with, children, help_needed, intent, meeting_reference,
# preferred_start_at, referrer_name, referrer_type, relationship_to_child,
# requesting_adult_name, service_requested, summary. Only `intent` and `summary`
# are persisted on the `Recording` row. The other eleven -- which include a
# child's name, who that child lives with, and the requesting adult's name --
# exist nowhere but the live analysis dict.
#
# A workflow hands a value from one step to the next through `StepResult` output,
# and the engine persists that output in `workflow_steps.output` and mirrors it
# into `workflow_events`. So splitting "analyse" from "submit to the CRM" would
# mean writing a child's name into the workflow audit trail, which is exactly
# what must not happen. Carrying it any other way would need either a new
# `Recording` column or an out-of-band cache, and neither is in scope.
#
# One step keeps the sensitive content inside the process that fetched it. What
# the run records is the outcome: identifiers and statuses, nothing a disclosure
# could be reconstructed from. Every AC-5 property -- idempotency, lease,
# retry with backoff, stale recovery, state transitions, audit -- is a property
# of the run, not of how finely it is sliced.
#
# This supersedes the readiness plan's three-step sketch, which was written
# before the thirteen keys were traced.

INTAKE = "voice.intake"
INTAKE_SHADOW = "voice.intake.shadow"
#: What the pipeline starts: the analysis is already persisted, so this records
#: the run and its audit trail without re-analysing and without submitting.
INTAKE_RECORDED = "voice.intake.recorded"

#: The fields the pipeline already carries for the model call, plus the two it
#: adds. Built from the row so the workflow hands `crm_sync` exactly what
#: `pipeline._run` hands it -- parity is at this boundary, and
#: `test_voice_intake_parity.py` asserts it.
CALL_PAYLOAD_FIELDS = (
    "ext_id",
    "call_date",
    "caller",
    "phone",
    "agent",
    "duration",
    "transcript",
)

#: What a step may return. Everything else is dropped before the engine sees it,
#: so a key added to an analysis upstream cannot leak into the audit trail by
#: default. Deliberately a whitelist: a denylist would need updating every time
#: the model's output grows a field.
SAFE_OUTPUT_KEYS = frozenset({
    "intent",
    "category",
    "route",
    "restricted",
    "priority",
    "risk",
    "sentiment",
    "needs_reply",
    "confidence",
    "escalate",
    "crm_status",
    "crm_reference",
    "activity_ref",
    "analysis_ok",
    "submitted",
    "reused",
})


def safe_output(**values: object) -> dict[str, object]:
    """Only the keys on the whitelist, and only scalars.

    The engine redacts what it writes, but `redact()` truncates a long string to
    500 characters rather than removing it, and 500 characters of a safeguarding
    disclosure is a disclosure. So nothing unsafe is handed to it in the first
    place.
    """
    out: dict[str, object] = {}
    for key, value in values.items():
        if key not in SAFE_OUTPUT_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
    return out


def _persisted_analysis(recording) -> dict[str, object] | None:
    """The analysis fields the pipeline has already written to the row.

    Deliberately the non-sensitive ones only: this feeds step output, and
    `summary` and `ai_reply` are the two persisted fields that carry content.
    `None` when the row has not been analysed, so a reuse run over an
    unanalysed recording fails rather than recording an empty outcome.
    """
    if getattr(recording, "analysis_status", "") != "done":
        return None
    return {
        "intent": recording.intent or "NONE",
        "category": recording.category or "unclassified",
        "route": recording.route or "",
        "priority": recording.priority or "",
        "risk": recording.risk or "",
        "sentiment": recording.sentiment or "",
        "needs_reply": bool(recording.needs_reply),
        "crm_status": recording.crm_status or "none",
    }


def build_handlers(*, session_factory=None):
    """The registry for this capability. One handler, registered by key.

    `session_factory` is injectable for the same reason `workflow_repository`
    is a named function: the parity harness runs this exact handler with a
    stand-in session, so what is tested is the shipped code rather than a
    re-implementation of it.
    """
    from platform_orchestration import HandlerRegistry, StepResult

    from . import agent_client, crm_sync

    sessions = session_factory or SessionLocal
    handlers = HandlerRegistry()

    @handlers.handler("voice.process_intake")
    async def process_intake(ctx):
        """Analyse one recording and forward the outcome to the CRM.

        Wraps the two existing calls verbatim -- `agent_client.analyze_call` and
        `crm_sync.after_call_analysis` -- and reimplements neither. The analysis
        dict never leaves this function.

        `config["submit"]` is the shadow switch. False computes everything and
        stops short of the CRM, which is what makes a parity run safe: the
        existing pipeline remains the only thing that submits.
        """
        submit = bool(ctx.step.config.get("submit", True))
        reuse = bool(ctx.step.config.get("reuse_persisted", False))

        async with sessions() as session:
            recording = await session.get(Recording, ctx.subject_ref)
            if recording is None:
                return StepResult.fail("no recording %s" % ctx.subject_ref)
            payload = {f: getattr(recording, f, "") or "" for f in CALL_PAYLOAD_FIELDS}
            persisted = _persisted_analysis(recording) if reuse else None

        if reuse:
            # The pipeline has already analysed this recording and committed the
            # result. Calling the model again would cost a second request, return
            # something subtly different -- a model call is not deterministic --
            # and make any comparison between the two paths meaningless. So the
            # run records what was persisted.
            #
            # Only possible while `submit` is false. `crm_sync.after_call_analysis`
            # reads eleven analysis keys that are never persisted (a child's name
            # among them), so a reused analysis cannot build a CRM payload. The
            # definition that reuses never submits, and `build_definitions`
            # enforces that pairing.
            if persisted is None:
                return StepResult.fail("no persisted analysis for %s" % ctx.subject_ref)
            return StepResult.ok(**safe_output(
                analysis_ok=True,
                submitted=False,
                reused=True,
                intent=persisted["intent"],
                category=persisted["category"],
                route=persisted["route"],
                priority=persisted["priority"],
                risk=persisted["risk"],
                sentiment=persisted["sentiment"],
                needs_reply=persisted["needs_reply"],
                crm_status=persisted["crm_status"],
            ))

        try:
            result = await agent_client.analyze_call(payload)
        except Exception as exc:  # noqa: BLE001
            # Transport or upstream failure: retryable, and the engine's backoff
            # decides when. The message is the exception type only -- an upstream
            # error body can quote the transcript back at us.
            ctx.logger.warning("analysis failed for %s: %s", ctx.subject_ref, type(exc).__name__)
            return StepResult.retry("analysis call failed: %s" % type(exc).__name__)

        analysis = result.get("analysis") or {}
        if not analysis:
            return StepResult.fail("analysis returned no content")

        # Persisted exactly as `pipeline._run` persists it, same fields, same
        # defaults. The CRM outcome is written in the same transaction, which is
        # what the pipeline does today.
        crm_status, crm_reference, activity_ref = "none", None, None
        if submit:
            crm_status, crm_reference, activity_ref = await crm_sync.after_call_analysis(
                payload, analysis
            )

        async with sessions() as session:
            recording = await session.get(Recording, ctx.subject_ref)
            if recording is None:
                return StepResult.fail("recording %s vanished" % ctx.subject_ref)
            recording.summary = analysis.get("summary", "")
            recording.category = analysis.get("category") or "unclassified"
            recording.flags = list(analysis.get("flags") or [])
            recording.route = analysis.get("route") or "exception"
            recording.priority = analysis.get("priority", "Medium")
            recording.risk = analysis.get("risk", "Low")
            recording.sentiment = analysis.get("sentiment", "Neutral")
            recording.needs_reply = bool(analysis.get("needs_reply", False))
            recording.ai_reply = analysis.get("suggested_reply", "") or ""
            recording.reply_status = "pending" if recording.needs_reply else "none"
            recording.analysis_status = "done"
            recording.intent = analysis.get("intent") or "NONE"
            if submit:
                recording.crm_status = crm_status
                recording.crm_reference = crm_reference
                recording.activity_ref = activity_ref
            await session.commit()

        return StepResult.ok(**safe_output(
            analysis_ok=True,
            submitted=submit,
            intent=analysis.get("intent") or "NONE",
            category=recording.category,
            route=recording.route,
            priority=recording.priority,
            risk=recording.risk,
            sentiment=recording.sentiment,
            needs_reply=recording.needs_reply,
            escalate=bool(result.get("escalate")),
            crm_status=crm_status,
            crm_reference=crm_reference,
            activity_ref=activity_ref,
        ))

    return handlers


def build_definitions(handlers):
    """`voice.intake` and its shadow twin.

    One handler, two definitions, differing only in `config["submit"]` -- the
    seam `StepSpec.config` exists for. No branch on an environment variable and
    no second code path, so a parity run exercises the same code the live path
    will.

    Neither step is declared `consequential`, and that is a deliberate
    declaration rather than an oversight -- the library requires one or the
    other. `WorkflowDefinition.validate` refuses a consequential step with no
    approval gate at or before it, because "a consequential step must be gated,
    or be declared non-consequential deliberately".

    It is declared non-consequential because that is accurate here.
    `consequential` means "effects outside the system that cannot be reversed".
    A submission to the CRM's `/intake/*` endpoints does not reach case data: CRM
    migration `60105e60dc0a` turned `intake_activities` into a staging record,
    and "nothing an agent sends reaches case data until somebody entitled to
    create the target record approves it". A coordinator rejecting the
    submission reverses it in full. The irreversible act happens in the CRM,
    behind the CRM's own human gate, and is recorded there.

    `requires_approval` is absent for the same reason: the CRM is the approval
    authority for intake, and gating here as well would put two humans in front
    of every call. If that decision is ever revisited, the change is to set both
    flags on this step -- the engine then stops in front of it rather than after.
    """
    from platform_orchestration import DefinitionRegistry, StepSpec, WorkflowDefinition

    definitions = DefinitionRegistry()
    for name, submit, reuse in (
        (INTAKE, True, False),
        (INTAKE_SHADOW, False, False),
        (INTAKE_RECORDED, False, True),
    ):
        # Enforced rather than trusted: a definition that reuses the persisted
        # analysis cannot submit, because eleven of the thirteen keys
        # `after_call_analysis` reads are never persisted. A pairing of
        # reuse+submit would send a referral with a child missing from it.
        if reuse and submit:
            raise AssertionError(
                "definition %r reuses the persisted analysis and submits; the "
                "persisted row cannot build a CRM payload" % name
            )
        definitions.register(
            WorkflowDefinition(
                name=name,
                capability=CAPABILITY,
                steps=(
                    StepSpec(
                        name="process",
                        handler="voice.process_intake",
                        consequential=False,
                        max_attempts=3,
                        config={"submit": submit, "reuse_persisted": reuse},
                    ),
                ),
            ),
            known_handlers=handlers.keys(),
        )
    return definitions


# ── starting a run, and allocating a retry ──────────────────────────────────

#: Namespace for the per-recording advisory lock. `hashtext` maps it to the
#: bigint `pg_try_advisory_xact_lock` takes.
LOCK_NAMESPACE = "voice.intake"

#: Returned instead of a run when another attempt holds the lock. The caller
#: turns this into the 409 `POST /recordings/{id}/retry-analysis` already
#: returns, so the endpoint's contract does not change.
ALREADY_RUNNING = object()


async def allocate_retry_key(session, subject_ref: str) -> str | None:
    """The idempotency key for a deliberate retry, allocated safely.

    Q-2's requirement was a distinct run per deliberate retry, without a
    `Recording` column and without a non-atomic count of existing runs. This is
    that, and the ordering is the whole design:

      1. `pg_try_advisory_xact_lock` -- a real mutex, not check-then-act. It
         returns false rather than waiting, which is what lets the caller answer
         409 immediately instead of holding a request open.
      2. the count, taken *while holding the mutex*, so it is atomic: no other
         caller can read the same number.
      3. the key, `<recording id>:<n>`, deterministic and legible in the audit
         trail.

    The lock is transaction-scoped on purpose. A session-scoped lock would have
    to be held across the repository's own transaction, and would be unsafe
    behind a transaction-mode pooler. The cost is that the mutex is released
    before the run is created, so two callers racing that window can mint the
    same key -- and `uq_workflow_idempotency` then collapses them to one run
    rather than erroring, which is the conservative direction: one run instead
    of two, never two submissions instead of one.

    Returns None when another attempt holds the lock.
    """
    from sqlalchemy import func, select as sa_select, text

    from platform_orchestration.models import WorkflowRunRow

    locked = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
        {"key": "%s:%s" % (LOCK_NAMESPACE, subject_ref)},
    )
    if not locked:
        log.info("retry refused: %s is already being processed", subject_ref)
        return None

    existing = await session.scalar(
        sa_select(func.count())
        .select_from(WorkflowRunRow)
        .where(
            WorkflowRunRow.capability == CAPABILITY,
            WorkflowRunRow.subject_ref == subject_ref,
        )
    )
    return "%s:%d" % (subject_ref, int(existing or 0))


async def start_intake(
    orchestrator: Orchestrator,
    subject_ref: str,
    *,
    principal: Principal,
    shadow: bool = False,
):
    """Start the intake workflow for one recording.

    The idempotency key is the recording id, so a scheduler tick that re-sees
    the same row -- or the stuck-pending re-queue finding one already in flight
    -- returns the existing run instead of starting a second analysis. That is
    what makes the existing self-heal safe rather than duplicative.
    """
    return await orchestrator.start(
        definition=INTAKE_SHADOW if shadow else INTAKE,
        subject_ref=subject_ref,
        principal=principal,
        idempotency_key=subject_ref,
        payload={"shadow": shadow},
    )


async def start_retry(
    orchestrator: Orchestrator,
    subject_ref: str,
    *,
    principal: Principal,
    session_factory=None,
    shadow: bool = False,
):
    """Start a *deliberate* retry: a new run, or `ALREADY_RUNNING`.

    Distinct from `start_intake` because the semantics differ. An automatic
    start must not duplicate; a person pressing "Generate summary" is asking for
    another attempt and must get one, or the button looks broken.
    """
    sessions = session_factory or SessionLocal
    async with sessions() as session:
        key = await allocate_retry_key(session, subject_ref)
        if key is None:
            return ALREADY_RUNNING
    return await orchestrator.start(
        definition=INTAKE_SHADOW if shadow else INTAKE,
        subject_ref=subject_ref,
        principal=principal,
        idempotency_key=key,
        payload={"shadow": shadow, "retry": True},
    )


def build_orchestrator(
    authorization: RecordingAuthorizationPort,
    *,
    repository=None,
    audit=None,
    session_factory=None,
) -> Orchestrator:
    """An orchestrator for one caller, over this service's own database.

    `repository` and `audit` are injectable so a test can run the real handlers
    and the real definitions against `InMemoryWorkflowRepository` without a
    database -- which is how the parity harness works.
    """
    handlers = build_handlers(session_factory=session_factory)
    return Orchestrator(
        repository=repository if repository is not None else workflow_repository(),
        definitions=build_definitions(handlers),
        handlers=handlers,
        authorization=authorization,
        audit=audit if audit is not None else audit_sink(),
    )
