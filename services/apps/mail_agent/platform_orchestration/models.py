"""SQLAlchemy tables for the workflow store.

Four tables, in the host service's own database -- which keeps the repo's
"one database per service" convention and means the orchestration layer needs no
new infrastructure of its own.

What these tables deliberately do NOT hold is business data. A run knows the
*reference* of the email, recording or case it concerns and nothing about its
contents, so the CRM stays authoritative for CRM records and the mail store for
mail. The one place working data is held is `workflow_steps.output`, which exists
so a step can hand a value to a later step in the same run; it is scoped to the
run, and audit redaction applies on the way into `workflow_events`.

Imported lazily by `sql.py` rather than from the package root, so a host that does
not persist workflows -- and the library's own test suite -- never needs SQLAlchemy
installed to use the engine.

Declared on their own `Base`. The host creates them alongside its own tables
(`OrchestrationBase.metadata.create_all`) rather than this library reaching into
the service's declarative base, which would couple the two in the wrong direction.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """The orchestration tables' own metadata."""


def _id() -> str:
    return str(uuid.uuid4())


#: The lifecycle states, as a database-level constraint. Duplicated from
#: `states.WorkflowState` on purpose: a CHECK constraint cannot reference Python,
#: and a state reaching the column that the lifecycle does not define should fail
#: at the write rather than be discovered when something tries to read it back.
#: `tests/test_sql_models.py` asserts the two lists agree, so the duplication
#: cannot drift silently.
_STATES = (
    "created", "running", "awaiting_approval", "approved", "rejected",
    "retry_scheduled", "succeeded", "failed", "exception", "cancelled",
)
_STATE_LIST = ", ".join("'%s'" % s for s in _STATES)


class WorkflowRunRow(Base):
    """One execution of one workflow definition over one business record."""

    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    definition: Mapped[str] = mapped_column(String)
    #: The definition version this run began on. A redeploy that changes a
    #: definition's steps must not silently re-point an in-flight run at the new
    #: shape, so the engine compares this and parks the run if it has moved.
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    capability: Mapped[str] = mapped_column(String)
    #: Reference to the business record. Not a foreign key: the referenced row
    #: lives in this service for mail, but in the CRM for a case-driven workflow,
    #: and a constraint that only holds for some capabilities is worse than none.
    subject_ref: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="created", server_default="created")
    step_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: Attempts spent on the *current* step, reset when the run moves on.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: A worker's claim. Expiry rather than an explicit release is what makes an
    #: interrupted worker recoverable: a killed process leaves a lease that
    #: lapses, not a lock held forever.
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Creation idempotency, unique per definition where set. This is the
    #: constraint that makes "a cron tick re-seeing the same email" safe at the
    #: database level and not merely in application logic.
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    started_by: Mapped[str | None] = mapped_column(String, nullable=True)
    queue: Mapped[str] = mapped_column(String, default="", server_default="")
    routing_rule: Mapped[str] = mapped_column(String, default="", server_default="")
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("definition", "idempotency_key", name="uq_workflow_idempotency"),
        CheckConstraint("state IN (%s)" % _STATE_LIST, name="ck_workflow_runs_state"),
        # The sweep queries. Both are narrow and frequent: one per scheduler tick.
        Index("ix_workflow_runs_due", "state", "next_attempt_at"),
        Index("ix_workflow_runs_lease", "state", "lease_expires_at"),
        Index("ix_workflow_runs_subject", "capability", "subject_ref"),
        Index("ix_workflow_runs_queue", "queue", "state"),
        Index("ix_workflow_runs_created", "created_at"),
    )


class WorkflowStepRow(Base):
    """The outcome of one step of one run.

    Unique on (run, name), which is what makes the duplicate-effect guard
    reliable: "has this step already succeeded?" is a single indexed lookup with
    exactly one possible answer.
    """

    __tablename__ = "workflow_steps"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="created", server_default="created")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("run_id", "name", name="uq_workflow_step"),
        CheckConstraint("state IN (%s)" % _STATE_LIST, name="ck_workflow_steps_state"),
        Index("ix_workflow_steps_run", "run_id"),
    )


class WorkflowApprovalRow(Base):
    """A human decision on a gated step, and who made it.

    Kept for rejections as well as approvals: a rejection is the outcome most
    likely to be asked about later, so it is recorded rather than represented by
    the absence of an approval. No UPDATE or DELETE path exists in the engine --
    the same append-only discipline the mail agent's `email_audit` already follows.
    """

    __tablename__ = "workflow_approvals"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE")
    )
    step: Mapped[str] = mapped_column(String)
    decision: Mapped[str] = mapped_column(String)
    decided_by: Mapped[str] = mapped_column(String)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("run_id", "step", name="uq_workflow_approval"),
        CheckConstraint(
            "decision IN ('approved','rejected')", name="ck_workflow_approval_decision"
        ),
        Index("ix_workflow_approvals_run", "run_id"),
        Index("ix_workflow_approvals_actor", "decided_by", "decided_at"),
    )


class WorkflowEventRow(Base):
    """Append-only audit trail.

    Every row arrives through `redact`, so message bodies, transcripts, drafts and
    credentials cannot be here. What is here is decisions, transitions, outcomes
    and source references -- enough to reconstruct what happened and why, which is
    the whole requirement.
    """

    __tablename__ = "workflow_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    #: Not a foreign key. An event about a run must survive the run being deleted;
    #: an audit trail that cascades away with its subject is not an audit trail.
    run_id: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    capability: Mapped[str] = mapped_column(String, default="", server_default="")
    subject_ref: Mapped[str] = mapped_column(String, default="", server_default="")
    step: Mapped[str | None] = mapped_column(String, nullable=True)
    from_state: Mapped[str | None] = mapped_column(String, nullable=True)
    to_state: Mapped[str | None] = mapped_column(String, nullable=True)
    actor_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    actor_kind: Mapped[str] = mapped_column(String, default="system", server_default="system")
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: "system:ref" strings. The provenance, never the content.
    sources: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_workflow_events_run", "run_id", "created_at"),
        Index("ix_workflow_events_action", "action", "created_at"),
        Index("ix_workflow_events_actor", "actor_user_id", "created_at"),
        Index("ix_workflow_events_subject", "subject_ref", "created_at"),
    )


#: Exported under a distinct name so a host importing it alongside its own `Base`
#: cannot confuse the two.
OrchestrationBase = Base

__all__ = [
    "Base", "OrchestrationBase", "WorkflowRunRow", "WorkflowStepRow",
    "WorkflowApprovalRow", "WorkflowEventRow",
]
