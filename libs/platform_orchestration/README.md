# platform_orchestration

Shared workflow orchestration for Project West capabilities: lifecycle, routing,
human approval, retry, recovery and audit, with no capability-specific business
logic in it.

Email uses it today (`services/apps/mail_agent/app/orchestration.py`). Voice,
Report Management, Scheduling, Invoicing and Case Management are meant to use the
same core without changing it.

## Why a library and not a service

The orchestration layer needs durable state, and `agent_factory` is deliberately
stateless. A separate `services/orchestrator` would mean another deployment,
another Neon database and a network hop for every state transition. So this is a
shared package installed into the capability service that executes the workflows,
persisting into **that service's own database** — which keeps the repo's "one
database per service" convention and adds no infrastructure.

Cross-capability work still crosses the service boundary the way everything else
in this repo does: an HTTP call with the internal key (see `agent_client.py`).
`RoutingTarget.capability` is how a routing rule says that is what should happen.

## Layout

| Module | What it holds |
|---|---|
| `states.py` | The lifecycle: states and the legal transitions between them |
| `definition.py` | `WorkflowDefinition` / `StepSpec` — workflows as data |
| `handlers.py` | `StepContext` / `StepResult` — what a capability writes |
| `engine.py` | `Orchestrator` — create, execute, approve, retry, recover |
| `repository.py` | Storage port + a complete in-memory implementation |
| `sql.py` | SQLAlchemy repository and audit sink (optional extra, `[sql]`) |
| `models.py` | The four tables, on their own declarative base |
| `routing.py` | Configurable rules, evaluated against caller-supplied facts |
| `principal.py` | `Principal` and the `AuthorizationPort` the host implements |
| `context.py` | Authorised context retrieval, with provenance |
| `audit.py` | Events, and the redaction that keeps content out of them |

## The four guarantees the engine enforces

1. **Nothing consequential runs before approval.** A step declared
   `requires_approval=True` parks the run in `awaiting_approval` *before* its
   handler is called. A definition with a `consequential=True` step and no
   approval gate in front of it fails to register at startup. `submit_approval`
   takes a human and refuses a service principal, so the decision is always
   somebody's.
2. **No step runs twice.** Execution is guarded by a database compare-and-set,
   and a step already recorded `succeeded` is skipped on resume.
3. **Every transition is legal and recorded**, through one table and one
   redacted audit event.
4. **An interrupted run is recoverable.** Work is leased, not locked, so a
   process that dies leaves a run that becomes claimable again.

## Adding a capability

Four pieces, none of which touch the engine. Taking Voice as the example:

```python
# 1. Handlers -- your business logic, registered by key.
from platform_orchestration import HandlerRegistry, StepResult

handlers = HandlerRegistry()

@handlers.handler("voice.transcribe")
async def transcribe(ctx):
    try:
        text = await bt_client.transcript(ctx.subject_ref)
    except TransientError as exc:
        return StepResult.retry(str(exc))      # retried within the step's budget
    except ValidationError as exc:
        return StepResult.fail(str(exc))       # permanent; never retried
    if text is None:
        return StepResult.needs_review("no transcript available")
    return StepResult.ok(transcript=text)      # passed to later steps
```

```python
# 2. A definition -- the steps as data, saying what needs approval.
from platform_orchestration import DefinitionRegistry, StepSpec, WorkflowDefinition

definitions = DefinitionRegistry()
definitions.register(
    WorkflowDefinition(
        name="voice.call_followup",
        capability="voice",
        steps=(
            StepSpec(name="transcribe", handler="voice.transcribe"),
            StepSpec(name="analyse", handler="voice.analyse"),
            StepSpec(
                name="send", handler="voice.send_followup",
                requires_approval=True, consequential=True, max_attempts=3,
            ),
        ),
    ),
    known_handlers=handlers.keys(),
)
```

```python
# 3. An authorization port over your own access rules. Async, because deciding
#    this usually needs a lookup. Every method must fail closed.
class RecordingAuthorizationPort:
    def __init__(self, scope):
        self._scope = scope

    async def can_view(self, principal, *, capability, subject_ref): ...
    async def can_start(self, principal, *, capability, subject_ref): ...
    async def can_approve(self, principal, *, capability, subject_ref): ...
```

```python
# 4. Wire it up, against your own database.
from platform_orchestration import Orchestrator
from platform_orchestration.sql import SqlAuditSink, SqlWorkflowRepository

orchestrator = Orchestrator(
    repository=SqlWorkflowRepository(session_factory=SessionLocal),
    definitions=definitions,
    handlers=handlers,
    authorization=RecordingAuthorizationPort(scope),
    audit=SqlAuditSink(session_factory=SessionLocal),
    routing=build_routing_table(),
)
```

Then create the tables alongside your own (`main.py`):

```python
from platform_orchestration.models import OrchestrationBase

async with engine.begin() as conn:
    await conn.run_sync(Base.metadata.create_all)
    await conn.run_sync(OrchestrationBase.metadata.create_all)
```

And drive it:

```python
run = await orchestrator.start(
    definition="voice.call_followup",
    subject_ref=recording_id,
    principal=principal,
    idempotency_key=recording_id,      # a repeat call returns the same run
)
run = await orchestrator.run_to_completion(run.id, principal=principal)
# -> stops at `awaiting_approval`; nothing consequential has run.

run = await orchestrator.approve_and_continue(
    run.id, principal=reviewer, approved=True, note="checked",
)
```

Finally, add the retry/recovery sweep to your existing scheduler tick — see
`mail_agent/app/scheduler.py:_workflow_tick`. Without it, a scheduled retry never
fires.

## When a capability is configured to act on its own

Some capabilities are deliberately unattended — the mail agent's auto-reply
setting is one, and an administrator switching it on *is* an authorisation
decision. The choice there is not "human approval or policy approval"; it is
"policy approval or **no record at all**".

So a step can opt in to being released by a standing policy:

```python
StepSpec(
    name="send", handler="voice.send_followup",
    requires_approval=True, consequential=True,
    policy_approval=True,          # off by default
)
```

```python
run = await orchestrator.policy_approve_and_continue(
    run.id, principal=SYSTEM_PRINCIPAL, policy="voice.auto_followup_enabled",
)
```

The approval row records `decided_by="policy:voice.auto_followup_enabled"` and the
audit action is `approved_by_policy`, so nothing can read it as a person pressing
Approve.

Three rules make this safe to have added:

- `submit_policy_approval` takes a **service** principal and refuses a human —
  the exact mirror of `submit_approval`. A person's decision is recorded as
  theirs.
- A step that has not set `policy_approval=True` is unreachable this way, so
  every gate that existed before still means "a human, or nothing".
- `policy_approval` without `requires_approval` fails to register, rather than
  sitting in a definition looking like a protection that is not there.

Give the autonomous flow its **own definition** rather than a flag on the shared
one. "Which runs did a person approve?" is then answerable by definition name, and
a policy can never release a run that was started from a review screen.

## Two things not to do

**Do not reimplement the taxonomy here.** Classification and its queue routing
belong to `agent_factory`, next to the vocabulary that defines them. Pass the
route you were given as a routing *fact*; do not recompute it. The mail adapter's
routing table answers a different question — given a queue, may a drafted reply be
offered for approval — and that is the shape to follow.

**Do not put business records in the workflow store.** A run holds a
`subject_ref` and nothing else about its subject. The CRM stays authoritative for
CRM records and SharePoint for documents; `ContextProvider` fetches what a step
needs, on behalf of the principal, and only the provenance is persisted.

## Testing

The library's own suite runs with no database and no network:

```bash
cd libs/platform_orchestration
python -m pytest -q
```

`InMemoryWorkflowRepository` is a complete implementation of the storage port, not
a stub, so the engine's concurrency and recovery behaviour is genuinely exercised.
Use it to test your handlers too — `mail_agent/tests/test_workflow_reply.py` is a
worked example.

## Vendored copies

Each service deploys from its own directory, so it carries a copy of this package
(the convention `platform_common` already follows). The canonical source is here;
`scripts/sync_shared_libs.py` refreshes the copies and `--check` fails if any has
drifted. A test in each adopting service's suite asserts the same thing, so a
stale copy fails a build rather than shipping.
