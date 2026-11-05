"""The durable shape of a workflow, and the storage port the engine talks to.

The engine never touches a database. It talks to a `WorkflowRepository`, which the
host backs with its own service database -- preserving the "one database per
service" convention and avoiding a new piece of infrastructure for the orchestration
layer to need.

The records here hold *references and control state*, never business data. A run
knows the id of the email or recording it concerns and nothing about its contents.
That is what keeps the CRM authoritative for CRM records and the mail store
authoritative for mail, and stops this layer becoming a second source of truth for
anything that already has one.

Two primitives in the port do the heavy lifting for reliability:

  * `claim` is a compare-and-set. It advances a run out of its current state only
    if the run is *still* in that state and no live lease is held on it. Two workers
    racing on the same run therefore produce exactly one winner, which is what
    prevents the same step -- and so the same outbound side effect -- executing
    twice. It is the same shape of guard the mail pipeline already applies to its
    "pending" rows, promoted from an in-process set to the database so it survives
    more than one process.

  * `find_by_idempotency_key` makes workflow *creation* idempotent. A cron tick
    that re-sees the same email, or a double-clicked button, finds the existing run
    instead of starting a second one over the same record.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from .states import WorkflowState


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class WorkflowRun:
    """One execution of one workflow definition over one business record.

    Mutable, unlike most types in this library: the engine advances a run through
    several fields at once and then persists it, and a frozen record would mean a
    `replace` per field change with no benefit -- the authorization and lifecycle
    guarantees are enforced by the engine and the state table, not by immutability
    here.
    """

    definition: str
    capability: str
    subject_ref: str
    id: str = field(default_factory=new_id)
    #: The definition version this run was started against. A deployment that
    #: changes a definition's steps must not silently re-point an in-flight run at
    #: the new shape; the engine refuses to resume a run whose version no longer
    #: matches, rather than executing a step list the run never began.
    version: int = 1
    state: WorkflowState = WorkflowState.CREATED
    #: Index into the definition's steps. The next step to run.
    step_index: int = 0
    #: Attempts spent on the *current* step. Reset when the run moves on.
    attempts: int = 0
    #: Set when the engine schedules a retry. `resume_due` finds runs whose time
    #: has come.
    next_attempt_at: datetime | None = None
    #: While held and unexpired, another worker will not claim this run. Expiry
    #: rather than an explicit release is what makes an interrupted worker
    #: recoverable: a killed process leaves a lease that lapses, not a lock that
    #: is held forever.
    lease_expires_at: datetime | None = None
    #: Creation idempotency. Unique per (definition, key) where set.
    idempotency_key: str | None = None
    #: Who started the run. "" for a scheduler.
    started_by: str | None = None
    #: The routing outcome, as a logical queue name.
    queue: str = ""
    routing_rule: str = ""
    #: Non-sensitive parameters the run was started with, passed to every handler.
    payload: dict[str, Any] = field(default_factory=dict)
    #: Routing annotations and context provenance ("system:ref" strings). Never
    #: record content.
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Operator-facing reason the run is in its current state, when that state is
    #: failed, exception or rejected.
    last_error: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None

    def touch(self) -> None:
        self.updated_at = utcnow()

    @property
    def is_waiting_for_approval(self) -> bool:
        return self.state is WorkflowState.AWAITING_APPROVAL


@dataclass
class StepRecord:
    """The outcome of one step of one run.

    Persisted per step rather than only on the run, for two reasons. It is the
    record a resumed run consults to know a step is already done and must not be
    repeated -- the duplicate-effect guard. And it is what makes a partial failure
    legible after the fact: "steps 1 and 2 succeeded, 3 failed on attempt 3" is a
    diagnosis, while a run that only says `failed` is not.
    """

    run_id: str
    name: str
    id: str = field(default_factory=new_id)
    state: WorkflowState = WorkflowState.CREATED
    attempts: int = 0
    #: Handler output, passed to later steps. May contain working data (a draft
    #: reply); audit redaction applies when any of it reaches an event.
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def succeeded(self) -> bool:
        return self.state is WorkflowState.SUCCEEDED


@dataclass
class ApprovalRecord:
    """A human decision on a gated step, and who made it.

    Kept even when the decision was "no": a rejection is the outcome most likely
    to be asked about later, so it is recorded rather than represented by the
    absence of an approval. Append-only in practice -- the engine writes one per
    decision and never updates one.
    """

    run_id: str
    step: str
    #: "approved" | "rejected"
    decision: str
    decided_by: str
    id: str = field(default_factory=new_id)
    #: Operator note. Redacted on its way into an audit event, so it is safe to
    #: let a reviewer type freely here.
    note: str | None = None
    decided_at: datetime = field(default_factory=utcnow)


@runtime_checkable
class WorkflowRepository(Protocol):
    """Storage the engine needs. Implemented in-memory here and over SQLAlchemy
    in `sql_repository`."""

    async def create_run(self, run: WorkflowRun) -> WorkflowRun: ...

    async def get_run(self, run_id: str) -> WorkflowRun | None: ...

    async def save_run(self, run: WorkflowRun) -> WorkflowRun: ...

    async def find_by_idempotency_key(
        self, *, definition: str, idempotency_key: str
    ) -> WorkflowRun | None: ...

    async def claim(
        self,
        run_id: str,
        *,
        expected_state: WorkflowState,
        to_state: WorkflowState,
        lease_until: datetime,
        now: datetime | None = None,
    ) -> WorkflowRun | None:
        """Compare-and-set. Returns the claimed run, or None if somebody else has it.

        None means "another worker is on this, or the run is no longer in the state
        you read" -- never an error. The caller's correct response is to leave it
        alone, which is what makes concurrent execution safe without a lock table.
        """

    async def release(self, run_id: str) -> None:
        """Drop the lease without changing state. For a worker finishing cleanly."""

    async def get_step(self, run_id: str, name: str) -> StepRecord | None: ...

    async def save_step(self, step: StepRecord) -> StepRecord: ...

    async def steps_for(self, run_id: str) -> Sequence[StepRecord]: ...

    async def record_approval(self, approval: ApprovalRecord) -> ApprovalRecord: ...

    async def approval_for(self, run_id: str, step: str) -> ApprovalRecord | None: ...

    async def due_runs(
        self, *, now: datetime | None = None, limit: int = 50
    ) -> Sequence[WorkflowRun]:
        """Runs whose scheduled retry time has arrived."""

    async def stale_runs(
        self, *, now: datetime | None = None, limit: int = 50
    ) -> Sequence[WorkflowRun]:
        """Runs left `running` by a worker that died, whose lease has lapsed."""

    async def list_runs(
        self,
        *,
        capability: str | None = None,
        states: Sequence[WorkflowState] | None = None,
        queue: str | None = None,
        subject_refs: Sequence[str] | None = None,
        limit: int = 100,
    ) -> Sequence[WorkflowRun]:
        """Query for the API layer.

        `subject_refs` exists so a host can constrain a listing to the records the
        caller is entitled to *in the query*, rather than filtering afterwards. An
        empty sequence means "nothing is permitted" and must return no rows -- not
        be treated as "no filter", which is the classic shape of this bug.
        """


class InMemoryWorkflowRepository:
    """A complete repository with no database.

    Not a stub: the engine's whole contract is exercised against this in the
    library's tests, including the compare-and-set and the retry sweep. It exists
    because the services in this repo test with fakes and monkeypatching rather
    than a live database (there is no sqlite driver installed in any service venv),
    and a storage port that can only be tested against Postgres is a port whose
    behaviour nobody checks.
    """

    def __init__(self) -> None:
        self._runs: dict[str, WorkflowRun] = {}
        self._steps: dict[tuple[str, str], StepRecord] = {}
        self._approvals: dict[tuple[str, str], ApprovalRecord] = {}
        self._by_key: dict[tuple[str, str], str] = {}

    # Copies in and out, so a caller mutating a returned run cannot reach into
    # storage behind the repository's back -- the SQL implementation gives out
    # detached copies too, and a fake whose aliasing differs from the real thing
    # hides exactly the bugs it is meant to catch.
    @staticmethod
    def _copy_run(run: WorkflowRun) -> WorkflowRun:
        return replace(run, payload=dict(run.payload), metadata=dict(run.metadata))

    @staticmethod
    def _copy_step(step: StepRecord) -> StepRecord:
        return replace(step, output=dict(step.output))

    async def create_run(self, run: WorkflowRun) -> WorkflowRun:
        self._runs[run.id] = self._copy_run(run)
        if run.idempotency_key:
            self._by_key[(run.definition, run.idempotency_key)] = run.id
        return self._copy_run(run)

    async def get_run(self, run_id: str) -> WorkflowRun | None:
        found = self._runs.get(run_id)
        return self._copy_run(found) if found else None

    async def save_run(self, run: WorkflowRun) -> WorkflowRun:
        run.touch()
        self._runs[run.id] = self._copy_run(run)
        if run.idempotency_key:
            self._by_key[(run.definition, run.idempotency_key)] = run.id
        return self._copy_run(run)

    async def find_by_idempotency_key(
        self, *, definition: str, idempotency_key: str
    ) -> WorkflowRun | None:
        run_id = self._by_key.get((definition, idempotency_key))
        return await self.get_run(run_id) if run_id else None

    async def claim(
        self,
        run_id: str,
        *,
        expected_state: WorkflowState,
        to_state: WorkflowState,
        lease_until: datetime,
        now: datetime | None = None,
    ) -> WorkflowRun | None:
        now = now or utcnow()
        stored = self._runs.get(run_id)
        if stored is None or stored.state is not expected_state:
            return None
        held = stored.lease_expires_at is not None and stored.lease_expires_at > now
        if held:
            return None
        stored.state = to_state
        stored.lease_expires_at = lease_until
        stored.updated_at = now
        return self._copy_run(stored)

    async def release(self, run_id: str) -> None:
        stored = self._runs.get(run_id)
        if stored is not None:
            stored.lease_expires_at = None

    async def get_step(self, run_id: str, name: str) -> StepRecord | None:
        found = self._steps.get((run_id, name))
        return self._copy_step(found) if found else None

    async def save_step(self, step: StepRecord) -> StepRecord:
        self._steps[(step.run_id, step.name)] = self._copy_step(step)
        return self._copy_step(step)

    async def steps_for(self, run_id: str) -> Sequence[StepRecord]:
        return tuple(
            self._copy_step(s) for (rid, _), s in sorted(self._steps.items())
            if rid == run_id
        )

    async def record_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        self._approvals[(approval.run_id, approval.step)] = approval
        return approval

    async def approval_for(self, run_id: str, step: str) -> ApprovalRecord | None:
        return self._approvals.get((run_id, step))

    async def due_runs(
        self, *, now: datetime | None = None, limit: int = 50
    ) -> Sequence[WorkflowRun]:
        now = now or utcnow()
        due = [
            self._copy_run(r) for r in self._runs.values()
            if r.state is WorkflowState.RETRY_SCHEDULED
            and r.next_attempt_at is not None
            and r.next_attempt_at <= now
        ]
        due.sort(key=lambda r: r.next_attempt_at or r.created_at)
        return tuple(due[:limit])

    async def stale_runs(
        self, *, now: datetime | None = None, limit: int = 50
    ) -> Sequence[WorkflowRun]:
        now = now or utcnow()
        stale = [
            self._copy_run(r) for r in self._runs.values()
            if r.state is WorkflowState.RUNNING
            and r.lease_expires_at is not None
            and r.lease_expires_at <= now
        ]
        stale.sort(key=lambda r: r.lease_expires_at or r.created_at)
        return tuple(stale[:limit])

    async def list_runs(
        self,
        *,
        capability: str | None = None,
        states: Sequence[WorkflowState] | None = None,
        queue: str | None = None,
        subject_refs: Sequence[str] | None = None,
        limit: int = 100,
    ) -> Sequence[WorkflowRun]:
        rows = list(self._runs.values())
        if capability is not None:
            rows = [r for r in rows if r.capability == capability]
        if states is not None:
            wanted = set(states)
            rows = [r for r in rows if r.state in wanted]
        if queue is not None:
            rows = [r for r in rows if r.queue == queue]
        if subject_refs is not None:
            # An empty sequence means "authorised for nothing" and returns nothing.
            # `is not None` rather than a truthiness test is the whole point.
            allowed = set(subject_refs)
            rows = [r for r in rows if r.subject_ref in allowed]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return tuple(self._copy_run(r) for r in rows[:limit])


def merge_metadata(existing: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """Shallow merge, new values winning. Kept here so the engine and the SQL
    repository agree on what "update the metadata" means."""
    merged = dict(existing)
    merged.update(new)
    return merged
