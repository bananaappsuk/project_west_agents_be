"""The SQLAlchemy-backed repository and audit sink.

Imported explicitly (`from platform_orchestration.sql import ...`) rather than from
the package root, so SQLAlchemy stays an optional dependency of the library.

Constructed with a **session factory**, not a session. Every method opens its own
short transaction and closes it, which is the discipline the mail agent's pipeline
already follows deliberately: the slow work in a workflow is an LLM call or a CRM
write inside a step handler, and a repository that held a pooled connection across
those would starve the pool under any real load.

The important method is `claim`. It is a single atomic `UPDATE ... WHERE state = ?
AND (lease has lapsed) RETURNING`, which is what makes concurrent execution safe
without a lock table: two workers issue the same statement, the database serialises
them, and exactly one row comes back changed. The in-memory repository implements
the same contract, so the engine's tests exercise the real semantics.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .audit import AuditEvent
from .models import (
    WorkflowApprovalRow,
    WorkflowEventRow,
    WorkflowRunRow,
    WorkflowStepRow,
)
from .repository import ApprovalRecord, StepRecord, WorkflowRun, utcnow
from .states import WorkflowState

log = logging.getLogger("platform_orchestration.sql")


# ── row <-> record mapping ───────────────────────────────────────────────────

def _run_from_row(row: WorkflowRunRow) -> WorkflowRun:
    return WorkflowRun(
        id=row.id,
        definition=row.definition,
        version=row.version,
        capability=row.capability,
        subject_ref=row.subject_ref,
        state=WorkflowState(row.state),
        step_index=row.step_index,
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        lease_expires_at=row.lease_expires_at,
        idempotency_key=row.idempotency_key,
        started_by=row.started_by,
        queue=row.queue,
        routing_rule=row.routing_rule,
        payload=dict(row.payload or {}),
        metadata=dict(row.metadata_json or {}),
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        finished_at=row.finished_at,
    )


def _apply_run(row: WorkflowRunRow, run: WorkflowRun) -> None:
    """Copy a record onto a row.

    `id`, `definition` and `subject_ref` are deliberately not written here. They
    are the run's identity and the reference it concerns, set once at creation --
    the same reasoning as the mail agent's immutable `mailbox_id`: a mutable
    authorization-relevant key is a vulnerability, because anything able to update
    it could re-point a run at a record its principal was never entitled to.
    """
    row.version = run.version
    row.capability = run.capability
    row.state = run.state.value
    row.step_index = run.step_index
    row.attempts = run.attempts
    row.next_attempt_at = run.next_attempt_at
    row.lease_expires_at = run.lease_expires_at
    row.idempotency_key = run.idempotency_key
    row.started_by = run.started_by
    row.queue = run.queue
    row.routing_rule = run.routing_rule
    row.payload = dict(run.payload)
    row.metadata_json = dict(run.metadata)
    row.last_error = run.last_error
    row.updated_at = run.updated_at or utcnow()
    row.finished_at = run.finished_at


def _step_from_row(row: WorkflowStepRow) -> StepRecord:
    return StepRecord(
        id=row.id,
        run_id=row.run_id,
        name=row.name,
        state=WorkflowState(row.state),
        attempts=row.attempts,
        output=dict(row.output or {}),
        error=row.error,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


class SqlWorkflowRepository:
    """`WorkflowRepository` over the host service's own Postgres database."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    # ── runs ─────────────────────────────────────────────────────────────────

    async def create_run(self, run: WorkflowRun) -> WorkflowRun:
        """Insert the run, or return the existing one on an idempotency clash.

        The unique constraint on (definition, idempotency_key) is the real
        guarantee, and this is where it pays off: two concurrent callers with the
        same key both reach here, one inserts, the other's insert raises, and the
        loser returns the winner's run rather than an error. The engine's
        pre-check is an optimisation; this is the correctness.
        """
        async with self._sessions() as session:
            row = WorkflowRunRow(
                id=run.id,
                definition=run.definition,
                version=run.version,
                capability=run.capability,
                subject_ref=run.subject_ref,
                state=run.state.value,
                step_index=run.step_index,
                attempts=run.attempts,
                next_attempt_at=run.next_attempt_at,
                lease_expires_at=run.lease_expires_at,
                idempotency_key=run.idempotency_key,
                started_by=run.started_by,
                queue=run.queue,
                routing_rule=run.routing_rule,
                payload=dict(run.payload),
                metadata_json=dict(run.metadata),
                last_error=run.last_error,
                created_at=run.created_at,
                updated_at=run.updated_at,
                finished_at=run.finished_at,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if run.idempotency_key:
                    existing = await self.find_by_idempotency_key(
                        definition=run.definition,
                        idempotency_key=run.idempotency_key,
                    )
                    if existing is not None:
                        log.info(
                            "workflow create raced on idempotency key; returning run %s",
                            existing.id,
                        )
                        return existing
                raise
            return _run_from_row(row)

    async def get_run(self, run_id: str) -> WorkflowRun | None:
        async with self._sessions() as session:
            row = await session.get(WorkflowRunRow, run_id)
            return _run_from_row(row) if row else None

    async def save_run(self, run: WorkflowRun) -> WorkflowRun:
        async with self._sessions() as session:
            row = await session.get(WorkflowRunRow, run.id)
            if row is None:
                raise LookupError("no workflow run %s to save" % run.id)
            run.touch()
            _apply_run(row, run)
            await session.commit()
            return _run_from_row(row)

    async def find_by_idempotency_key(
        self, *, definition: str, idempotency_key: str
    ) -> WorkflowRun | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(WorkflowRunRow).where(
                    WorkflowRunRow.definition == definition,
                    WorkflowRunRow.idempotency_key == idempotency_key,
                )
            )
            return _run_from_row(row) if row else None

    async def claim(
        self,
        run_id: str,
        *,
        expected_state: WorkflowState,
        to_state: WorkflowState,
        lease_until: datetime,
        now: datetime | None = None,
    ) -> WorkflowRun | None:
        """Atomic compare-and-set. One winner, whatever the concurrency.

        Returning None is a normal answer meaning "somebody else has it, or it
        moved on since you read it" -- and leaving it alone is the caller's
        correct response. This is what stops the same step, and so the same
        outbound side effect, happening twice.
        """
        now = now or utcnow()
        async with self._sessions() as session:
            result = await session.execute(
                update(WorkflowRunRow)
                .where(
                    WorkflowRunRow.id == run_id,
                    WorkflowRunRow.state == expected_state.value,
                    or_(
                        WorkflowRunRow.lease_expires_at.is_(None),
                        WorkflowRunRow.lease_expires_at <= now,
                    ),
                )
                .values(
                    state=to_state.value,
                    lease_expires_at=lease_until,
                    updated_at=now,
                )
                .returning(WorkflowRunRow.id)
            )
            claimed = result.scalar_one_or_none()
            if claimed is None:
                await session.rollback()
                return None
            await session.commit()
        return await self.get_run(run_id)

    async def release(self, run_id: str) -> None:
        async with self._sessions() as session:
            await session.execute(
                update(WorkflowRunRow)
                .where(WorkflowRunRow.id == run_id)
                .values(lease_expires_at=None)
            )
            await session.commit()

    # ── steps ────────────────────────────────────────────────────────────────

    async def get_step(self, run_id: str, name: str) -> StepRecord | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(WorkflowStepRow).where(
                    WorkflowStepRow.run_id == run_id,
                    WorkflowStepRow.name == name,
                )
            )
            return _step_from_row(row) if row else None

    async def save_step(self, step: StepRecord) -> StepRecord:
        async with self._sessions() as session:
            row = await session.scalar(
                select(WorkflowStepRow).where(
                    WorkflowStepRow.run_id == step.run_id,
                    WorkflowStepRow.name == step.name,
                )
            )
            if row is None:
                row = WorkflowStepRow(
                    id=step.id, run_id=step.run_id, name=step.name,
                )
                session.add(row)
            row.state = step.state.value
            row.attempts = step.attempts
            row.output = dict(step.output)
            row.error = step.error
            row.started_at = step.started_at
            row.finished_at = step.finished_at
            await session.commit()
            return _step_from_row(row)

    async def steps_for(self, run_id: str) -> Sequence[StepRecord]:
        async with self._sessions() as session:
            rows = (await session.scalars(
                select(WorkflowStepRow)
                .where(WorkflowStepRow.run_id == run_id)
                .order_by(WorkflowStepRow.started_at.asc().nullslast(),
                          WorkflowStepRow.name.asc())
            )).all()
            return tuple(_step_from_row(r) for r in rows)

    # ── approvals ────────────────────────────────────────────────────────────

    async def record_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        async with self._sessions() as session:
            session.add(WorkflowApprovalRow(
                id=approval.id,
                run_id=approval.run_id,
                step=approval.step,
                decision=approval.decision,
                decided_by=approval.decided_by,
                note=approval.note,
                decided_at=approval.decided_at,
            ))
            await session.commit()
            return approval

    async def approval_for(self, run_id: str, step: str) -> ApprovalRecord | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(WorkflowApprovalRow).where(
                    WorkflowApprovalRow.run_id == run_id,
                    WorkflowApprovalRow.step == step,
                )
            )
            if row is None:
                return None
            return ApprovalRecord(
                id=row.id, run_id=row.run_id, step=row.step,
                decision=row.decision, decided_by=row.decided_by,
                note=row.note, decided_at=row.decided_at,
            )

    # ── sweeps ───────────────────────────────────────────────────────────────

    async def due_runs(
        self, *, now: datetime | None = None, limit: int = 50
    ) -> Sequence[WorkflowRun]:
        now = now or utcnow()
        async with self._sessions() as session:
            rows = (await session.scalars(
                select(WorkflowRunRow)
                .where(
                    WorkflowRunRow.state == WorkflowState.RETRY_SCHEDULED.value,
                    WorkflowRunRow.next_attempt_at.is_not(None),
                    WorkflowRunRow.next_attempt_at <= now,
                )
                .order_by(WorkflowRunRow.next_attempt_at.asc())
                .limit(limit)
            )).all()
            return tuple(_run_from_row(r) for r in rows)

    async def stale_runs(
        self, *, now: datetime | None = None, limit: int = 50
    ) -> Sequence[WorkflowRun]:
        now = now or utcnow()
        async with self._sessions() as session:
            rows = (await session.scalars(
                select(WorkflowRunRow)
                .where(
                    WorkflowRunRow.state == WorkflowState.RUNNING.value,
                    WorkflowRunRow.lease_expires_at.is_not(None),
                    WorkflowRunRow.lease_expires_at <= now,
                )
                .order_by(WorkflowRunRow.lease_expires_at.asc())
                .limit(limit)
            )).all()
            return tuple(_run_from_row(r) for r in rows)

    async def list_runs(
        self,
        *,
        capability: str | None = None,
        states: Sequence[WorkflowState] | None = None,
        queue: str | None = None,
        subject_refs: Sequence[str] | None = None,
        limit: int = 100,
    ) -> Sequence[WorkflowRun]:
        async with self._sessions() as session:
            stmt = select(WorkflowRunRow)
            if capability is not None:
                stmt = stmt.where(WorkflowRunRow.capability == capability)
            if states is not None:
                stmt = stmt.where(
                    WorkflowRunRow.state.in_([s.value for s in states])
                )
            if queue is not None:
                stmt = stmt.where(WorkflowRunRow.queue == queue)
            if subject_refs is not None:
                # `is not None`, not truthiness: an empty authorised set means
                # "entitled to nothing" and must return nothing. Treating it as
                # "no filter" is the classic shape of this bug, and `in_([])`
                # correctly matches no rows.
                stmt = stmt.where(WorkflowRunRow.subject_ref.in_(list(subject_refs)))
            stmt = stmt.order_by(WorkflowRunRow.created_at.desc()).limit(limit)
            rows = (await session.scalars(stmt)).all()
            return tuple(_run_from_row(r) for r in rows)


class SqlAuditSink:
    """Writes audit events to `workflow_events`.

    Events arrive already redacted -- the engine emits only `event.redacted()` --
    so this cannot be the path by which content reaches the table.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def record(self, event: AuditEvent) -> None:
        async with self._sessions() as session:
            session.add(WorkflowEventRow(
                run_id=event.run_id,
                action=event.action,
                capability=event.capability,
                subject_ref=event.subject_ref,
                step=event.step,
                from_state=event.from_state,
                to_state=event.to_state,
                actor_user_id=event.actor_user_id,
                actor_kind=event.actor_kind,
                attempt=event.attempt,
                # A JSONB column, so the list is stored as a JSON array rather
                # than needing a Postgres ARRAY type and its own migration.
                sources=list(event.sources) or None,
                detail=dict(event.detail) or None,
                created_at=event.created_at,
            ))
            await session.commit()


__all__ = ["SqlWorkflowRepository", "SqlAuditSink"]
