"""Append-only workflow events, with sensitive content kept out of them.

Two requirements pull in opposite directions here, so the resolution is explicit:
enough must be recorded to investigate a failure, and nothing sensitive may be
exposed through the record. The settlement is that events carry *decisions,
transitions, outcomes and references* -- never message bodies, transcripts, draft
replies, case notes or credentials.

`redact` enforces that mechanically rather than by reviewer vigilance. A handler
returning `{"body": ...}` in its output has that key replaced, not because the
handler was wrong to produce it but because the audit trail is the wrong place for
it. The record it belongs to is still reachable through `subject_ref`, where the
existing per-record access controls apply; the audit trail does not become a
second, unprotected copy of the content.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger("platform_orchestration.audit")

#: Keys whose values never appear in an event, matched case-insensitively against
#: the whole key name. Deliberately broad: the cost of redacting a field that
#: would have been harmless is nil, and the cost of leaking one is not.
SENSITIVE_KEYS: frozenset[str] = frozenset({
    "body", "html", "text", "content", "transcript", "summary",
    "draft", "draft_reply", "suggested_reply", "reply", "message",
    "notes", "note", "attachment", "attachments",
    "password", "password_enc", "secret", "client_secret", "token",
    "access_token", "refresh_token", "api_key", "authorization",
    "key", "credential", "credentials", "pem", "private_key",
})

#: Substrings that mark a key sensitive even inside a longer name, so
#: `smtp_password`, `graph_client_secret` and `email_body` are all caught without
#: enumerating every composite a capability might invent.
SENSITIVE_FRAGMENTS: tuple[str, ...] = (
    "password", "secret", "token", "api_key", "apikey", "credential",
    "private", "transcript", "body",
)

_REDACTED = "[redacted]"

#: Hard ceiling on any single string in an event. A stack trace or a provider
#: error message is useful for diagnosis and unbounded in length; truncating
#: keeps one pathological error from filling the audit table, and keeps a quoted
#: fragment of a message body from arriving inside an exception string.
MAX_STRING = 500

#: Ceiling on nesting. Beyond this the subtree is summarised rather than walked:
#: an event records a decision, it does not serialise an object graph.
MAX_DEPTH = 4

#: Ceiling on sequence length in an event.
MAX_ITEMS = 20


def _is_sensitive(key: str) -> bool:
    k = key.lower()
    if k in SENSITIVE_KEYS:
        return True
    return any(frag in k for frag in SENSITIVE_FRAGMENTS)


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Return `value` with sensitive keys removed and long strings truncated.

    Structure is preserved so an event stays readable: a redacted field is present
    with the value `"[redacted]"` rather than absent, because "this field was set
    and we are not showing it" and "this field was never set" are different facts
    when reconstructing what happened.
    """
    if _depth > MAX_DEPTH:
        return "[truncated: too deeply nested]"

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            out[key] = _REDACTED if _is_sensitive(key) else redact(v, _depth=_depth + 1)
        return out

    if isinstance(value, (list, tuple, set, frozenset)):
        # Bounded as well as redacted -- a thousand-element list in an event is
        # never the useful part of it.
        seq = list(value)
        items = [redact(v, _depth=_depth + 1) for v in seq[:MAX_ITEMS]]
        if len(seq) > MAX_ITEMS:
            items.append("[+%d more]" % (len(seq) - MAX_ITEMS))
        return items

    if isinstance(value, bool) or value is None:
        return value

    if isinstance(value, (int, float)):
        return value

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + "[truncated]"

    # Anything else is reduced to its type name rather than trusted to have a safe
    # repr -- an ORM object's repr can carry column values.
    return "<%s>" % type(value).__name__


@dataclass(frozen=True)
class AuditEvent:
    """One thing that happened to one workflow run.

    `action` is a stable verb ("created", "step_started", "step_succeeded",
    "approval_requested", "approved", "rejected", "retry_scheduled",
    "transition", "failed", "duplicate_suppressed"). Stable because queries and
    alerting key off it.
    """

    run_id: str
    action: str
    capability: str = ""
    subject_ref: str = ""
    step: str | None = None
    from_state: str | None = None
    to_state: str | None = None
    actor_user_id: str | None = None
    actor_kind: str = "system"   # "system" | "user" | "service"
    attempt: int | None = None
    #: Provenance of any context the step used, as "system:ref" strings. The
    #: references, never the content.
    sources: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def redacted(self) -> AuditEvent:
        """A copy safe to persist or log. The engine emits only these."""
        return AuditEvent(
            run_id=self.run_id,
            action=self.action,
            capability=self.capability,
            subject_ref=self.subject_ref,
            step=self.step,
            from_state=self.from_state,
            to_state=self.to_state,
            actor_user_id=self.actor_user_id,
            actor_kind=self.actor_kind,
            attempt=self.attempt,
            sources=self.sources,
            detail=redact(self.detail),
            created_at=self.created_at,
        )


@runtime_checkable
class AuditSink(Protocol):
    """Where events go. The host decides; the engine only emits."""

    async def record(self, event: AuditEvent) -> None:
        ...


class LoggingAuditSink:
    """Writes events to the service logger.

    Useful on its own in development and as a second destination in production.
    Events reaching it are already redacted by the engine, so this cannot become
    the hole through which content reaches a log aggregator.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._log = logger or log

    async def record(self, event: AuditEvent) -> None:
        self._log.info(
            "AUDIT workflow=%s action=%s step=%s %s->%s actor=%s/%s attempt=%s "
            "sources=%s detail=%s",
            event.run_id, event.action, event.step, event.from_state, event.to_state,
            event.actor_kind, event.actor_user_id or "-", event.attempt,
            ",".join(event.sources) or "-", event.detail,
        )


class InMemoryAuditSink:
    """Collects events in a list. The library's own tests assert against this."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)

    def actions(self) -> list[str]:
        return [e.action for e in self.events]

    def for_action(self, action: str) -> list[AuditEvent]:
        return [e for e in self.events if e.action == action]


class FanOutAuditSink:
    """Writes each event to several sinks.

    A failing sink must not take the workflow down with it: an audit destination
    being unreachable is a reason to shout, not a reason to abandon a run that has
    already had side effects. So a sink that raises is logged and the remaining
    sinks still receive the event. The durable sink is listed first by convention
    so it is never the one skipped.
    """

    def __init__(self, sinks: Iterable[AuditSink]) -> None:
        self._sinks = list(sinks)

    async def record(self, event: AuditEvent) -> None:
        for sink in self._sinks:
            try:
                await sink.record(event)
            except Exception:
                log.exception("audit sink %s failed for run=%s action=%s",
                              type(sink).__name__, event.run_id, event.action)
