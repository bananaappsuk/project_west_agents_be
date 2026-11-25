"""Shared workflow orchestration for Project West capabilities.

The foundation Email, Voice, Report Management, Scheduling, Invoicing and Case
Management all initiate and execute workflows through. A capability supplies three
things and gets lifecycle management, routing, approval gating, retry, recovery and
an audit trail without implementing any of them:

  1. **Step handlers** -- its own business logic, registered by key
     (`HandlerRegistry`), returning a `StepResult`.
  2. **A workflow definition** -- its steps as data (`WorkflowDefinition`), saying
     which need human approval and which are consequential.
  3. **Ports** -- a repository for its own database, an `AuthorizationPort` over
     its own access rules, and optionally a `ContextProvider`.

The engine imports nothing from any capability, and no capability imports the
engine's internals, so the Voice Agent can be added later against this same
interface without the orchestration core changing.

    from platform_orchestration import (
        Orchestrator, WorkflowDefinition, StepSpec, StepResult,
        DefinitionRegistry, HandlerRegistry, InMemoryWorkflowRepository,
    )

    handlers = HandlerRegistry()

    @handlers.handler("mail.classify")
    async def classify(ctx):
        return StepResult.ok(category="new_referral")

    definitions = DefinitionRegistry()
    definitions.register(
        WorkflowDefinition(
            name="mail.triage",
            capability="mail",
            steps=(
                StepSpec(name="classify", handler="mail.classify"),
                StepSpec(name="send", handler="mail.send",
                         requires_approval=True, consequential=True),
            ),
        ),
        known_handlers=handlers.keys(),
    )

    orchestrator = Orchestrator(
        repository=InMemoryWorkflowRepository(),
        definitions=definitions,
        handlers=handlers,
        authorization=my_port,
    )

The SQLAlchemy-backed repository is imported from `platform_orchestration.sql`
rather than from here, so a host that does not persist workflows -- or a test
suite -- never needs SQLAlchemy to import this package.
"""

from .audit import (
    AuditEvent,
    AuditSink,
    FanOutAuditSink,
    InMemoryAuditSink,
    LoggingAuditSink,
    redact,
)
from .context import (
    ContextBundle,
    ContextFragment,
    ContextProvider,
    ContextRequest,
    NullContextProvider,
    SourceReference,
    StaticContextProvider,
)
from .definition import (
    DEFAULT_MAX_ATTEMPTS,
    DefinitionRegistry,
    StepSpec,
    WorkflowDefinition,
)
from .engine import Orchestrator
from .errors import (
    DefinitionError,
    InvalidTransition,
    NotAuthorized,
    OrchestrationError,
    UnknownHandler,
    WorkflowNotFound,
)
from .handlers import (
    HandlerRegistry,
    StepContext,
    StepHandler,
    StepOutcome,
    StepResult,
)
from .principal import (
    SYSTEM_PRINCIPAL,
    AllowAll,
    AuthorizationPort,
    DenyAll,
    Principal,
)
from .repository import (
    ApprovalRecord,
    InMemoryWorkflowRepository,
    StepRecord,
    WorkflowRepository,
    WorkflowRun,
)
from .routing import (
    RoutingDecision,
    RoutingFacts,
    RoutingRule,
    RoutingTable,
    RoutingTarget,
)
from .states import (
    TERMINAL,
    TRANSITIONS,
    WAITING,
    WorkflowState,
    assert_transition,
    can_transition,
    is_terminal,
)

__all__ = [
    # lifecycle
    "WorkflowState", "TRANSITIONS", "TERMINAL", "WAITING",
    "can_transition", "assert_transition", "is_terminal",
    # definitions
    "WorkflowDefinition", "StepSpec", "DefinitionRegistry", "DEFAULT_MAX_ATTEMPTS",
    # handlers
    "HandlerRegistry", "StepContext", "StepResult", "StepOutcome", "StepHandler",
    # engine
    "Orchestrator",
    # storage
    "WorkflowRepository", "InMemoryWorkflowRepository",
    "WorkflowRun", "StepRecord", "ApprovalRecord",
    # authorization
    "Principal", "AuthorizationPort", "DenyAll", "AllowAll", "SYSTEM_PRINCIPAL",
    # context
    "ContextProvider", "ContextRequest", "ContextBundle", "ContextFragment",
    "SourceReference", "NullContextProvider", "StaticContextProvider",
    # routing
    "RoutingTable", "RoutingRule", "RoutingFacts", "RoutingTarget", "RoutingDecision",
    # audit
    "AuditEvent", "AuditSink", "LoggingAuditSink", "InMemoryAuditSink",
    "FanOutAuditSink", "redact",
    # errors
    "OrchestrationError", "InvalidTransition", "WorkflowNotFound", "NotAuthorized",
    "UnknownHandler", "DefinitionError",
]
