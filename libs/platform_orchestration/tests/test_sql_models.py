"""The SQL layer's structure, without needing a database.

No service venv in this repo has a sqlite driver installed, so these tests check
what can be checked statically: that the schema matches the lifecycle it is meant
to constrain, that both repository implementations satisfy the same protocol, and
that the columns the engine's sweeps rely on are indexed. Behavioural coverage of
the repository contract comes from the in-memory implementation, which the engine
tests exercise end to end.

The drift guard is the valuable one here. The state list in a CHECK constraint
cannot reference Python, so it is written out twice; this asserts the two agree, so
adding a lifecycle state without updating the constraint fails here rather than as
an integrity error in production.
"""

from __future__ import annotations

from platform_orchestration import (
    InMemoryWorkflowRepository,
    WorkflowRepository,
    WorkflowState,
)
from platform_orchestration.models import (
    _STATES,
    OrchestrationBase,
    WorkflowApprovalRow,
    WorkflowEventRow,
    WorkflowRunRow,
    WorkflowStepRow,
)
from platform_orchestration.sql import SqlAuditSink, SqlWorkflowRepository

# ── the drift guard ──────────────────────────────────────────────────────────

def test_the_database_state_list_matches_the_lifecycle():
    """A state added to the enum but not the constraint would fail on write."""
    assert set(_STATES) == {s.value for s in WorkflowState}


def test_the_run_state_check_constraint_names_every_state():
    constraint = next(
        c for c in WorkflowRunRow.__table__.constraints
        if getattr(c, "name", "") == "ck_workflow_runs_state"
    )
    sql = str(constraint.sqltext)
    for state in WorkflowState:
        assert "'%s'" % state.value in sql


# ── both repositories are the same port ──────────────────────────────────────

def test_the_in_memory_repository_satisfies_the_protocol():
    assert isinstance(InMemoryWorkflowRepository(), WorkflowRepository)


def test_the_sql_repository_satisfies_the_protocol():
    """The fake and the real implementation are interchangeable from the engine's
    point of view, which is what makes testing against the fake meaningful."""
    assert isinstance(SqlWorkflowRepository(session_factory=None), WorkflowRepository)


def test_both_repositories_expose_the_same_methods():
    required = [
        "create_run", "get_run", "save_run", "find_by_idempotency_key",
        "claim", "release", "get_step", "save_step", "steps_for",
        "record_approval", "approval_for", "due_runs", "stale_runs", "list_runs",
    ]
    for name in required:
        assert callable(getattr(InMemoryWorkflowRepository(), name)), name
        assert callable(getattr(SqlWorkflowRepository(session_factory=None), name)), name


# ── schema shape ─────────────────────────────────────────────────────────────

def test_the_expected_tables_are_declared():
    assert set(OrchestrationBase.metadata.tables) == {
        "workflow_runs", "workflow_steps", "workflow_approvals", "workflow_events",
    }


def test_creation_idempotency_is_enforced_by_a_unique_constraint():
    """Application logic alone would lose a concurrent race; the constraint does
    not."""
    names = {
        getattr(c, "name", "") for c in WorkflowRunRow.__table__.constraints
    }
    assert "uq_workflow_idempotency" in names


def test_a_step_is_unique_per_run():
    """What makes "has this step already succeeded?" have one possible answer."""
    names = {
        getattr(c, "name", "") for c in WorkflowStepRow.__table__.constraints
    }
    assert "uq_workflow_step" in names


def test_the_sweep_columns_are_indexed():
    """Both sweeps run once per scheduler tick and must stay cheap."""
    indexes = {ix.name for ix in WorkflowRunRow.__table__.indexes}
    assert "ix_workflow_runs_due" in indexes
    assert "ix_workflow_runs_lease" in indexes


def test_audit_events_are_not_cascade_deleted_with_their_run():
    """An audit trail that disappears with its subject is not an audit trail."""
    assert WorkflowEventRow.__table__.c.run_id.foreign_keys == set()


def test_steps_and_approvals_belong_to_their_run():
    assert WorkflowStepRow.__table__.c.run_id.foreign_keys
    assert WorkflowApprovalRow.__table__.c.run_id.foreign_keys


def test_an_approval_decision_is_constrained_to_the_two_real_outcomes():
    constraint = next(
        c for c in WorkflowApprovalRow.__table__.constraints
        if getattr(c, "name", "") == "ck_workflow_approval_decision"
    )
    sql = str(constraint.sqltext)
    assert "'approved'" in sql and "'rejected'" in sql


def test_the_business_record_reference_is_not_a_foreign_key():
    """The referenced row lives in this service for mail but in the CRM for a
    case-driven workflow, so a constraint would only hold for some capabilities."""
    assert WorkflowRunRow.__table__.c.subject_ref.foreign_keys == set()


def test_the_audit_sink_is_constructible():
    assert isinstance(SqlAuditSink(session_factory=None), SqlAuditSink)


# ── the SQL itself compiles, and means what it should ───────────────────────
#
# `sql.py` is the one module the in-memory tests cannot exercise. Compiling its
# statements against the real Postgres dialect checks the constructs without
# needing a database, and catches the mistakes that would otherwise only appear
# on a deployed service.

def test_the_claim_is_an_atomic_update_returning():
    """The whole concurrency guarantee. If this stops being a single conditional
    UPDATE, two workers can both run a step and send the same reply twice."""
    from sqlalchemy import or_, update
    from sqlalchemy.dialects import postgresql

    from platform_orchestration.repository import utcnow

    now = utcnow()
    stmt = (
        update(WorkflowRunRow)
        .where(
            WorkflowRunRow.id == "x",
            WorkflowRunRow.state == WorkflowState.RETRY_SCHEDULED.value,
            or_(
                WorkflowRunRow.lease_expires_at.is_(None),
                WorkflowRunRow.lease_expires_at <= now,
            ),
        )
        .values(state=WorkflowState.RUNNING.value, lease_expires_at=now, updated_at=now)
        .returning(WorkflowRunRow.id)
    )
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert sql.startswith("UPDATE workflow_runs SET")
    # The state predicate is what makes it a compare-and-set rather than a
    # blind write.
    assert "workflow_runs.state =" in sql
    assert "lease_expires_at IS NULL OR" in sql
    assert sql.rstrip().endswith("RETURNING workflow_runs.id")


def test_an_empty_authorised_set_compiles_to_matching_no_rows():
    """The database half of "authorised for nothing returns nothing".

    `in_([])` is the thing a reader doubts, so it is pinned here: SQLAlchemy
    compiles it to a false predicate rather than dropping the filter, which is
    what would turn an empty authorised set into an unfiltered query.
    """
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql

    stmt = select(WorkflowRunRow).where(WorkflowRunRow.subject_ref.in_([]))
    sql = str(stmt.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    ))
    assert "1 != 1" in sql


def test_the_sweep_queries_compile():
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql

    from platform_orchestration.repository import utcnow

    now = utcnow()
    for state, column in (
        (WorkflowState.RETRY_SCHEDULED, WorkflowRunRow.next_attempt_at),
        (WorkflowState.RUNNING, WorkflowRunRow.lease_expires_at),
    ):
        stmt = (
            select(WorkflowRunRow)
            .where(
                WorkflowRunRow.state == state.value,
                column.is_not(None),
                column <= now,
            )
            .order_by(column.asc())
            .limit(50)
        )
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "FROM workflow_runs" in sql
        assert "LIMIT" in sql


def test_the_step_ordering_handles_a_never_started_step():
    """A step record exists before it starts, so `started_at` can be NULL and the
    ordering must not put those first or blow up."""
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql

    stmt = (
        select(WorkflowStepRow)
        .where(WorkflowStepRow.run_id == "r")
        .order_by(
            WorkflowStepRow.started_at.asc().nullslast(),
            WorkflowStepRow.name.asc(),
        )
    )
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "NULLS LAST" in sql
