"""The gateway's routing table is an integration contract, not a convenience.

Every request the browser makes arrives here first, so a prefix missing from
`ROUTES` is indistinguishable to the user from the feature not existing: the
gateway answers 404 before the owning service is ever consulted. That is how the
approval gate's own endpoints came to be unreachable -- `/workflows` was absent,
so a reply could be queued and never released.

These tests pin the prefixes the frontend actually calls. The list is taken from
the endpoint map in the frontend's `lib/mockApi.ts`.
"""

from app.config import settings
from app.main import ROUTES, _upstream_for

#: Every path the frontend requests, and the service that must answer it.
#: Paths are written as the browser sends them, without a leading slash, which
#: is what `_upstream_for` receives from the catch-all route.
FRONTEND_CALLS: dict[str, str] = {
    # --- auth ---
    "auth/entra": settings.auth_url,
    "auth/refresh": settings.auth_url,
    "auth/me": settings.auth_url,
    "auth/logout": settings.auth_url,
    ".well-known/jwks.json": settings.auth_url,
    # --- mail agent: email surface ---
    "emails": settings.mail_agent_url,
    "emails/fetch": settings.mail_agent_url,
    "emails/move": settings.mail_agent_url,
    "emails/archive": settings.mail_agent_url,
    "emails/unarchive": settings.mail_agent_url,
    "emails/auto-reply": settings.mail_agent_url,
    "emails/analyze-backlog": settings.mail_agent_url,
    "emails/abc-123/reply": settings.mail_agent_url,
    "emails/abc-123/reply/dismiss": settings.mail_agent_url,
    "emails/abc-123/retry-summary": settings.mail_agent_url,
    "mailboxes": settings.mail_agent_url,
    "folders": settings.mail_agent_url,
    "folders/f1": settings.mail_agent_url,
    "alerts": settings.mail_agent_url,
    "agent/status": settings.mail_agent_url,
    "settings/agent": settings.mail_agent_url,
    "settings/mailbox": settings.mail_agent_url,
    # --- mail agent: the approval gate ---
    "workflows/review-queue": settings.mail_agent_url,
    "workflows/reply-approval": settings.mail_agent_url,
    "workflows/run-abc": settings.mail_agent_url,
    "workflows/run-abc/approve": settings.mail_agent_url,
    "workflows/run-abc/reject": settings.mail_agent_url,
    "workflows/run-abc/cancel": settings.mail_agent_url,
    # --- other apps ---
    "voice/recordings": settings.voice_agent_url,
    "agents/mail-agent/summarise/invoke": settings.agent_factory_url,
}


def test_every_endpoint_the_frontend_calls_is_routed():
    unroutable = {p for p in FRONTEND_CALLS if _upstream_for(p) is None}
    assert not unroutable, (
        "the gateway answers 404 for these before any service sees them: %r"
        % sorted(unroutable)
    )


def test_each_endpoint_reaches_the_service_that_owns_it():
    wrong = {
        path: "%s (expected %s)" % (_upstream_for(path), expected)
        for path, expected in FRONTEND_CALLS.items()
        if _upstream_for(path) != expected
    }
    assert not wrong, "routed to the wrong upstream: %r" % wrong


def test_the_approval_gate_is_reachable_at_all():
    """Called out on its own because queueing and releasing live behind different
    prefixes: `/emails/{id}/reply` queues, `/workflows/{run}/approve` releases.
    Routing only the first is worse than routing neither -- replies accumulate at
    the gate with no way to let them through."""
    assert _upstream_for("emails/x/reply") == settings.mail_agent_url
    assert _upstream_for("workflows/run/approve") == settings.mail_agent_url


def test_an_unknown_prefix_is_still_refused():
    """The table is an allowlist. A new service must be added deliberately."""
    for path in ("nope", "internal/metrics", ""):
        assert _upstream_for(path) is None, path


def test_a_dotdot_segment_still_picks_the_upstream_by_its_literal_prefix():
    """Documents current behaviour, which is reported rather than endorsed.

    `_upstream_for` matches the raw prefix, and the proxy then builds
    `f"{upstream}/{path}"`, which httpx normalises -- so `emails/../auth/me`
    is sent to the mail agent as `/auth/me`. A caller can therefore choose which
    of the four configured upstreams receives a given path.

    It is not a way through the approval gate: the gateway makes no
    authorisation decisions, and the mail agent verifies the token and the
    `can_approve` grant itself before releasing a reply. The practical effect is
    reaching a different path on a service that will still refuse it.

    Left as-is because it is pre-existing and outside this ticket. If it is
    hardened (normalise the path before matching, reject `..`), this test should
    fail and be rewritten to assert the refusal -- that is the point of pinning
    it.
    """
    assert _upstream_for("emails/../auth/me") == settings.mail_agent_url
    assert _upstream_for("auth/../workflows/x/approve") == settings.auth_url


def test_longest_prefix_wins():
    """`/agents` (agent factory) and `/agent/` (mail agent) differ by one
    character. The table is sorted longest-first so the specific one matches."""
    assert _upstream_for("agents/x/y/invoke") == settings.agent_factory_url
    assert _upstream_for("agent/status") == settings.mail_agent_url


def test_the_table_is_sorted_longest_first():
    """Guards the guard: the resolution above is only correct while the table
    stays ordered, and `ROUTES` is built by a sort that a later edit could drop."""
    lengths = [len(prefix) for prefix, _ in ROUTES]
    assert lengths == sorted(lengths, reverse=True), ROUTES
