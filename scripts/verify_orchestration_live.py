"""Verify the orchestration layer against a real PostgreSQL database.

Everything in the two automated suites runs against `InMemoryWorkflowRepository`,
which is a complete implementation of the storage port but not the one that ships.
The behaviour that only a real database can demonstrate is the part the whole
design leans on: the atomic compare-and-set claim, the unique constraint behind
creation idempotency, lease expiry across processes, and JSONB audit persistence.

This script exercises those against Postgres and cleans up after itself.

Safety
------

It never touches existing tables. It creates a throwaway **schema**
(`pw_orch_verify_<timestamp>`), points the connection's `search_path` at it so
`create_all` lands there, and drops it with CASCADE at the end -- including when a
check fails. Nothing in `public` is read or written, which is what makes it safe to
point at a shared development server, and it works through a connection pooler
where `CREATE DATABASE` is refused.

It will not read a service's `.env`. The URL must be passed explicitly, so no
invocation can pick up a database nobody chose.

Usage
-----

    # prove the checks themselves are right, with no database at all
    python scripts/verify_orchestration_live.py --self-test

    # the real thing
    python scripts/verify_orchestration_live.py \
        --url "postgresql+asyncpg://USER:PASSWORD@HOST:5432/DBNAME"

Exit code is 0 only if every check passed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "libs" / "platform_orchestration"))

from platform_orchestration import (  # noqa: E402
    AllowAll,
    DefinitionRegistry,
    HandlerRegistry,
    InMemoryAuditSink,
    InMemoryWorkflowRepository,
    Orchestrator,
    Principal,
    StepResult,
    StepSpec,
    WorkflowDefinition,
    WorkflowState,
)
from platform_orchestration.repository import utcnow  # noqa: E402

S = WorkflowState
ALICE = Principal(user_id="alice")
CRON = Principal(user_id="", service=True)
POLICY = "verify.autonomous_enabled"

CAPABILITY = "verify"
HUMAN_GATED = "verify.human_gated"
POLICY_GATED = "verify.policy_gated"


class Checks:
    """Records pass/fail per named check instead of stopping at the first one.

    A single failure usually says something specific about the database (a missing
    constraint, a dialect difference), and the other answers are still worth having
    when diagnosing it.
    """

    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, ok, detail))
        print("  %-4s %s%s" % ("PASS" if ok else "FAIL", name,
                               "" if not detail else "  (%s)" % detail))

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.results if not ok)


def build_registries() -> tuple[DefinitionRegistry, HandlerRegistry, dict]:
    """Two definitions and three handlers, mirroring the mail agent's shape.

    Deliberately not importing the mail agent's own definitions: this verifies the
    *library* against a database, and pulling in a capability would drag its
    models and configuration along with it.
    """
    handlers = HandlerRegistry()
    state: dict = {"calls": [], "fail_once": set(), "hang": set()}

    @handlers.handler("verify.prepare")
    async def prepare(ctx):
        state["calls"].append(("prepare", ctx.subject_ref, ctx.attempt))
        return StepResult.ok(prepared=True)

    @handlers.handler("verify.send")
    async def send(ctx):
        state["calls"].append(("send", ctx.subject_ref, ctx.attempt))
        if ctx.subject_ref in state["fail_once"]:
            state["fail_once"].discard(ctx.subject_ref)
            return StepResult.retry("a transient failure, once")
        return StepResult.ok(sent=True)

    definitions = DefinitionRegistry()
    for name, policy_approval in ((HUMAN_GATED, False), (POLICY_GATED, True)):
        definitions.register(
            WorkflowDefinition(
                name=name,
                capability=CAPABILITY,
                steps=(
                    StepSpec(name="prepare", handler="verify.prepare", max_attempts=1),
                    StepSpec(
                        name="send", handler="verify.send",
                        requires_approval=True, consequential=True,
                        policy_approval=policy_approval, max_attempts=3,
                    ),
                ),
            ),
            known_handlers=handlers.keys(),
        )
    return definitions, handlers, state


async def run_checks(repo, audit, checks: Checks, *, label: str) -> None:
    """The scenario. Identical against Postgres and against the in-memory port."""
    definitions, handlers, state = build_registries()

    def orchestrator():
        # A fresh orchestrator per logical "process", which is what makes the
        # lease and claim checks meaningful rather than a single object talking
        # to itself.
        return Orchestrator(
            repository=repo, definitions=definitions, handlers=handlers,
            authorization=AllowAll(), audit=audit,
            backoff_base=0.0, backoff_cap=0.0,
        )

    print("\n%s" % label)

    # ── 1. persistence and the lifecycle ────────────────────────────────────
    orch = orchestrator()
    run = await orch.start(
        definition=HUMAN_GATED, subject_ref="subject-1", principal=ALICE,
        idempotency_key="subject-1", payload={"body": "hello"},
    )
    checks.record("a run persists and is readable back", run.id is not None)

    run = await orch.run_to_completion(run.id, principal=ALICE)
    checks.record(
        "it parks at the approval gate",
        run.state is S.AWAITING_APPROVAL,
        "state=%s" % run.state.value,
    )
    checks.record(
        "nothing consequential ran before approval",
        ("send", "subject-1", 1) not in state["calls"],
    )

    reread = await orchestrator().get(run.id, principal=ALICE)
    checks.record(
        "the parked state survives a new orchestrator",
        reread.state is S.AWAITING_APPROVAL,
    )

    # ── 2. creation idempotency, enforced by the database ───────────────────
    again = await orchestrator().start(
        definition=HUMAN_GATED, subject_ref="subject-1", principal=ALICE,
        idempotency_key="subject-1",
    )
    checks.record(
        "a repeated start returns the same run",
        again.id == run.id,
        "unique constraint on (definition, idempotency_key)",
    )

    concurrent = await asyncio.gather(*[
        orchestrator().start(
            definition=HUMAN_GATED, subject_ref="subject-race", principal=ALICE,
            idempotency_key="subject-race",
        )
        for _ in range(5)
    ], return_exceptions=True)
    ids = {r.id for r in concurrent if not isinstance(r, Exception)}
    checks.record(
        "five concurrent starts produce one run",
        len(ids) == 1 and not any(isinstance(r, Exception) for r in concurrent),
        "%d distinct run id(s)" % len(ids),
    )

    # ── 3. human approval releases it, and is recorded ──────────────────────
    released = await orchestrator().approve_and_continue(
        run.id, principal=ALICE, approved=True, note="verified",
    )
    checks.record(
        "approval releases the gated step",
        released.state is S.SUCCEEDED,
        "state=%s" % released.state.value,
    )
    approval = await repo.approval_for(run.id, "send")
    checks.record(
        "the approval is persisted against the run",
        approval is not None and approval.decided_by == "alice",
    )

    steps = await repo.steps_for(run.id)
    checks.record(
        "both step records persisted",
        {s.name for s in steps} == {"prepare", "send"},
        ", ".join(sorted("%s=%s" % (s.name, s.state.value) for s in steps)),
    )

    # ── 4. duplicate execution prevention ───────────────────────────────────
    before = sum(1 for c in state["calls"] if c[:2] == ("send", "subject-1"))
    finished = await orchestrator().advance(run.id, principal=ALICE)
    after = sum(1 for c in state["calls"] if c[:2] == ("send", "subject-1"))
    checks.record(
        "advancing a finished run runs nothing again",
        before == after and finished.state is S.SUCCEEDED,
    )

    # ── 5. the atomic claim, under real concurrency ─────────────────────────
    wrong_state_id = await _parked_run_id(orchestrator(), "subject-claim")
    refused = await repo.claim(
        wrong_state_id,
        # The run is parked in `awaiting_approval`; claiming it as `approved`
        # must fail rather than overwrite the state.
        expected_state=S.APPROVED,
        to_state=S.RUNNING,
        lease_until=utcnow() + timedelta(seconds=60),
    )
    checks.record(
        "claim refuses a run in the wrong state",
        refused is None,
        "compare-and-set, not a blind write",
    )

    race_id = await _parked_run_id(orchestrator(), "subject-claim2")
    await orchestrator().submit_approval(race_id, principal=ALICE, approved=True)
    winners = await asyncio.gather(*[
        repo.claim(
            race_id,
            expected_state=S.APPROVED,
            to_state=S.RUNNING,
            lease_until=utcnow() + timedelta(seconds=60),
        )
        for _ in range(8)
    ])
    checks.record(
        "eight concurrent claims produce exactly one winner",
        sum(1 for w in winners if w is not None) == 1,
        "%d winner(s)" % sum(1 for w in winners if w is not None),
    )
    await repo.release(race_id)

    # ── 6. retry scheduling and the due sweep ───────────────────────────────
    retry_id = await _parked_run_id(orchestrator(), "subject-retry")
    state["fail_once"].add("subject-retry")
    await orchestrator().approve_and_continue(
        retry_id, principal=ALICE, approved=True,
    )
    final = await orchestrator().get(retry_id, principal=ALICE)
    checks.record(
        "a transient failure is retried and then succeeds",
        final.state is S.SUCCEEDED,
        "attempts=%d" % final.attempts,
    )

    due_id = await _parked_run_id(orchestrator(), "subject-due")
    await orchestrator().submit_approval(due_id, principal=ALICE, approved=True)
    parked = await repo.get_run(due_id)
    parked.state = S.RETRY_SCHEDULED
    parked.next_attempt_at = utcnow() - timedelta(seconds=5)
    await repo.save_run(parked)
    resumed = await orchestrator().resume_due(principal=CRON)
    checks.record(
        "resume_due picks up a run whose retry is due",
        due_id in {r.id for r in resumed},
        "%d resumed" % len(resumed),
    )

    # ── 7. lease expiry and crash recovery ──────────────────────────────────
    stale_id = await _parked_run_id(orchestrator(), "subject-stale")
    await orchestrator().submit_approval(stale_id, principal=ALICE, approved=True)
    abandoned = await repo.get_run(stale_id)
    abandoned.state = S.RUNNING
    abandoned.lease_expires_at = utcnow() - timedelta(seconds=5)
    await repo.save_run(abandoned)

    stale = await repo.stale_runs(limit=50)
    checks.record(
        "a lapsed lease makes a run visible to recovery",
        stale_id in {r.id for r in stale},
        "%d stale" % len(stale),
    )
    recovered = await orchestrator().recover_stale(principal=CRON)
    checks.record(
        "recover_stale reschedules it",
        stale_id in {r.id for r in recovered},
    )

    # ── 8. the policy gate and its sweep ────────────────────────────────────
    auto = await orchestrator().start(
        definition=POLICY_GATED, subject_ref="subject-auto", principal=CRON,
        idempotency_key="subject-auto",
    )
    auto = await orchestrator().run_to_completion(auto.id, principal=CRON)
    checks.record(
        "a policy-gated run also parks before its consequential step",
        auto.state is S.AWAITING_APPROVAL,
    )
    swept = await orchestrator().resume_policy_gated(
        policy=POLICY, principal=CRON, definition=POLICY_GATED,
    )
    checks.record(
        "the policy sweep releases a stranded autonomous run",
        auto.id in {r.id for r in swept},
    )
    auto_approval = await repo.approval_for(auto.id, "send")
    checks.record(
        "the policy is recorded as the authority, not a person",
        auto_approval is not None
        and auto_approval.decided_by == "policy:%s" % POLICY,
        auto_approval.decided_by if auto_approval else "no approval row",
    )

    human_parked = await _parked_run_id(orchestrator(), "subject-untouched")
    await orchestrator().resume_policy_gated(
        policy=POLICY, principal=CRON, definition=POLICY_GATED,
    )
    untouched = await orchestrator().get(human_parked, principal=ALICE)
    checks.record(
        "the policy sweep leaves a human gate alone",
        untouched.state is S.AWAITING_APPROVAL,
    )

    # ── 9. audit persistence ────────────────────────────────────────────────
    if hasattr(audit, "actions"):
        actions = set(audit.actions())
    else:
        actions = await _persisted_actions(repo)
    for expected in ("created", "approval_requested", "approved",
                     "approved_by_policy", "step_succeeded"):
        checks.record("audit event persisted: %s" % expected, expected in actions)


async def _parked_run_id(orch, subject: str) -> str:
    run = await orch.start(
        definition=HUMAN_GATED, subject_ref=subject, principal=ALICE,
        idempotency_key=subject,
    )
    run = await orch.run_to_completion(run.id, principal=ALICE)
    return run.id


async def _persisted_actions(repo) -> set[str]:
    """Read the audit actions back out of the database the sink wrote them to."""
    from sqlalchemy import select

    from platform_orchestration.models import WorkflowEventRow

    async with repo._sessions() as session:   # noqa: SLF001
        rows = await session.scalars(select(WorkflowEventRow.action))
        return set(rows.all())


async def live(url: str) -> int:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from platform_orchestration.models import OrchestrationBase
    from platform_orchestration.sql import SqlAuditSink, SqlWorkflowRepository

    schema = "pw_orch_verify_%s" % utcnow().strftime("%Y%m%d%H%M%S")
    print("Throwaway schema: %s  (dropped on exit, nothing in public is touched)"
          % schema)

    admin = create_async_engine(
        url,
        isolation_level="AUTOCOMMIT",
        connect_args={"statement_cache_size": 0},
    )
    try:
        async with admin.connect() as conn:
            await conn.execute(text('CREATE SCHEMA "%s"' % schema))
    except Exception as exc:
        # A wrong password, an unreachable host or a role without CREATE on the
        # database all land here. Reported as one line rather than a traceback:
        # the cause is the connection, and the stack adds nothing.
        print("\nCould not prepare the throwaway schema.")
        print("  %s: %s" % (type(exc).__name__, str(exc).splitlines()[0][:200]))
        print("\nThe URL needs a role that may CREATE SCHEMA on that database.")
        print("Nothing was created, and no existing table was touched.")
        return 2
    finally:
        await admin.dispose()

    # `search_path` is what puts create_all and every query inside the throwaway
    # schema without the models needing to know about it.
    engine = create_async_engine(
        url,
        connect_args={
            "server_settings": {"search_path": schema},
            "statement_cache_size": 0,
        },
    )
    checks = Checks()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(OrchestrationBase.metadata.create_all)
        checks.record("startup create_all builds the workflow tables", True)

        async with engine.connect() as conn:
            found = await conn.scalars(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :s ORDER BY table_name"
            ), {"s": schema})
            tables = set(found.all())
        checks.record(
            "all four tables exist",
            tables == {"workflow_runs", "workflow_steps",
                       "workflow_approvals", "workflow_events"},
            ", ".join(sorted(tables)) or "none",
        )

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        repo = SqlWorkflowRepository(session_factory=session_factory)
        await run_checks(repo, SqlAuditSink(session_factory=session_factory),
                         checks, label="Against PostgreSQL:")
    finally:
        await engine.dispose()
        admin = create_async_engine(
        url,
        isolation_level="AUTOCOMMIT",
        connect_args={"statement_cache_size": 0},
    )
        try:
            async with admin.connect() as conn:
                await conn.execute(text('DROP SCHEMA "%s" CASCADE' % schema))
            print("\nThrowaway schema %s dropped." % schema)
        finally:
            await admin.dispose()

    return _report(checks)


async def self_test() -> int:
    """The same scenario against the in-memory port, so the checks can be trusted
    before they are pointed at a database.

    If this passes and the live run fails, the difference is the database, which
    is the whole point of running the live one.
    """
    checks = Checks()
    await run_checks(
        InMemoryWorkflowRepository(), InMemoryAuditSink(), checks,
        label="Against the in-memory repository (no database):",
    )
    return _report(checks)


def _report(checks: Checks) -> int:
    total = len(checks.results)
    print("\n%d/%d checks passed." % (total - checks.failed, total))
    if checks.failed:
        print("FAILED:")
        for name, ok, detail in checks.results:
            if not ok:
                print("  - %s%s" % (name, "" if not detail else "  (%s)" % detail))
    return 1 if checks.failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--url",
        help="SQLAlchemy async Postgres URL "
             "(postgresql+asyncpg://user:pass@host:port/dbname). Required unless "
             "--self-test. Never read from a service's .env.",
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help="run the same checks against the in-memory repository, no database",
    )
    args = parser.parse_args()

    if args.self_test:
        return asyncio.run(self_test())
    if not args.url:
        parser.error("--url is required (or use --self-test)")
    if not args.url.startswith("postgresql+asyncpg://"):
        parser.error(
            "--url must be a postgresql+asyncpg:// URL; got %r" % args.url[:24]
        )
    return asyncio.run(live(args.url))


if __name__ == "__main__":
    sys.exit(main())
