"""The voice orchestration authorization boundary.

Two things are being held here, and both are the kind that fail silently.

**Capability isolation.** The voice service and the mail agent share one
database and one `workflow_runs` table. `list_runs(capability=...)` is optional
on the engine, so isolation cannot rest on every call site remembering it. The
port refuses a foreign capability outright, and these tests are what say so.

**Safeguarding restriction.** Orchestration must not become a new path to a
safeguarding disclosure. `can_view` refuses a restricted recording to every
human principal, `{app}:admin` included, and refuses it when the answer is not
knowable at all.

No database: the port takes a session factory, so a stand-in answers the one
query `is_restricted` makes.

    PW-free; run with the service's own suite:
    .venv/Scripts/python.exe -m pytest tests/test_voice_authorization.py -q
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from platform_orchestration import SYSTEM_PRINCIPAL, Principal

from app.orchestration import (
    CAPABILITY,
    RESTRICTED_ROUTE,
    RecordingAuthorizationPort,
    is_restricted,
    principal_from_claims,
)

#: Module-level, matching the convention the existing voice tests use
#: (`test_crm_callback.py`), rather than adding `asyncio_mode` to the shared
#: pytest.ini -- that would change how every other test in this service is
#: collected, which is not this change's business.
pytestmark = pytest.mark.asyncio

#: The handful of tests below that exercise pure functions are declared
#: `async` purely so the module-level marker applies to them without a
#: warning. They await nothing -- the marker is a collection concern.

APP = "voice-agent"
ADMIN = f"{APP}:admin"
READ = f"{APP}:recordings.read"
WRITE = f"{APP}:recordings.write"

OPEN_REC = "rec-open"
RESTRICTED_REC = "rec-restricted"
MISSING_REC = "rec-missing"

ROUTES = {
    OPEN_REC: "case_communication",
    RESTRICTED_REC: RESTRICTED_ROUTE,
    # MISSING_REC deliberately absent
}


class _FakeSession:
    """Answers the one `select(Recording.route).where(id == ...)` the port makes."""

    def __init__(self, routes: dict[str, str], *, raises: bool = False):
        self.routes = routes
        self.raises = raises
        self.asked: list[str] = []

    async def scalar(self, statement):
        if self.raises:
            raise RuntimeError("database unavailable")
        # The recording id is the only bind parameter in the statement.
        params = statement.compile().params
        rec_id = next(iter(params.values()))
        self.asked.append(rec_id)
        return self.routes.get(rec_id)


def _factory(routes: dict[str, str] | None = None, *, raises: bool = False):
    session = _FakeSession(ROUTES if routes is None else routes, raises=raises)

    @asynccontextmanager
    async def factory():
        yield session

    factory.session = session  # type: ignore[attr-defined]
    return factory


def _port(**kwargs) -> RecordingAuthorizationPort:
    return RecordingAuthorizationPort(APP, session_factory=_factory(**kwargs))


def human(*scopes: str, user_id: str = "user-1") -> Principal:
    return Principal(user_id=user_id, scopes=frozenset(scopes))


# ── capability isolation ────────────────────────────────────────────────────
@pytest.mark.parametrize("method", ["can_start", "can_view", "can_approve"])
@pytest.mark.parametrize("capability", ["mail", "report", "", "Voice", "voice "])
async def test_no_method_answers_for_another_capability(method, capability):
    """The choke point. A mail run must not come back through a voice port.

    Includes near-misses -- wrong case, trailing space -- because a comparison
    that normalised would be a comparison that could be fooled.
    """
    port = _port()
    decide = getattr(port, method)
    assert await decide(
        human(ADMIN, READ, WRITE), capability=capability, subject_ref=OPEN_REC
    ) is False


@pytest.mark.parametrize("method", ["can_start", "can_view"])
async def test_the_system_principal_is_also_refused_another_capability(method):
    """Isolation is not a user-level rule; the scheduler is bound by it too."""
    port = _port()
    assert await getattr(port, method)(
        SYSTEM_PRINCIPAL, capability="mail", subject_ref=OPEN_REC
    ) is False


async def test_the_capability_name_is_voice():
    assert CAPABILITY == "voice"


# ── can_start: the pipeline and the retry endpoint ──────────────────────────
@pytest.mark.parametrize("rec", [OPEN_REC, RESTRICTED_REC])
async def test_the_pipeline_may_start_a_run_over_any_recording(rec):
    """Including a restricted one: an unanalysed safeguarding call is worse."""
    port = _port()
    assert await port.can_start(
        SYSTEM_PRINCIPAL, capability=CAPABILITY, subject_ref=rec
    ) is True


async def test_a_write_holder_may_start_a_run():
    port = _port()
    assert await port.can_start(
        human(WRITE), capability=CAPABILITY, subject_ref=OPEN_REC
    ) is True


async def test_a_write_holder_may_still_retry_a_restricted_recording():
    """Existing access, deliberately preserved.

    `POST /recordings/{id}/retry-analysis` works on a restricted recording
    today. Refusing it here would narrow existing access, which is a different
    decision from not widening it.
    """
    port = _port()
    assert await port.can_start(
        human(WRITE), capability=CAPABILITY, subject_ref=RESTRICTED_REC
    ) is True


async def test_a_read_only_user_may_not_start_a_run():
    port = _port()
    assert await port.can_start(
        human(READ), capability=CAPABILITY, subject_ref=OPEN_REC
    ) is False


async def test_the_admin_scope_may_start_a_run():
    """The bypass `app/deps.py` already applies, mirrored and not widened."""
    port = _port()
    assert await port.can_start(
        human(ADMIN), capability=CAPABILITY, subject_ref=OPEN_REC
    ) is True


async def test_an_unauthenticated_principal_may_not_start_a_run():
    port = _port()
    nobody = Principal(user_id="", scopes=frozenset({WRITE}))
    assert await port.can_start(
        nobody, capability=CAPABILITY, subject_ref=OPEN_REC
    ) is False


# ── can_view: fail closed on restriction ────────────────────────────────────
async def test_a_read_holder_may_view_an_unrestricted_run():
    port = _port()
    assert await port.can_view(
        human(READ), capability=CAPABILITY, subject_ref=OPEN_REC
    ) is True


async def test_a_read_holder_may_not_view_a_restricted_run():
    port = _port()
    assert await port.can_view(
        human(READ), capability=CAPABILITY, subject_ref=RESTRICTED_REC
    ) is False


async def test_the_admin_scope_may_not_view_a_restricted_run():
    """The point of the whole boundary.

    `{app}:admin` bypasses every other scope check in this service. It does not
    bypass this one: the administrative role that configures the system is not
    the clinical role that handles disclosures -- the mail agent's
    `safeguarding_clearance` exists for exactly that reason.
    """
    port = _port()
    assert await port.can_view(
        human(ADMIN), capability=CAPABILITY, subject_ref=RESTRICTED_REC
    ) is False


async def test_the_admin_scope_may_still_view_an_unrestricted_run():
    """Restriction is the only thing admin does not bypass."""
    port = _port()
    assert await port.can_view(
        human(ADMIN), capability=CAPABILITY, subject_ref=OPEN_REC
    ) is True


async def test_a_scopeless_user_may_not_view_anything():
    port = _port()
    assert await port.can_view(
        human(), capability=CAPABILITY, subject_ref=OPEN_REC
    ) is False


async def test_a_run_over_an_unknown_recording_is_not_viewable():
    """Fail closed: nothing legitimate asks about a recording that is not there."""
    port = _port()
    assert await port.can_view(
        human(READ), capability=CAPABILITY, subject_ref=MISSING_REC
    ) is False


async def test_a_failed_lookup_withholds_rather_than_permits():
    """The direction that matters. 'Not known' must not read as 'not restricted'."""
    port = _port(raises=True)
    assert await port.can_view(
        human(READ, ADMIN), capability=CAPABILITY, subject_ref=OPEN_REC
    ) is False


async def test_the_system_principal_may_view_its_own_runs():
    """So the scheduler can advance a run it started over a restricted recording."""
    port = _port()
    assert await port.can_view(
        SYSTEM_PRINCIPAL, capability=CAPABILITY, subject_ref=RESTRICTED_REC
    ) is True


# ── can_approve ─────────────────────────────────────────────────────────────
async def test_the_system_principal_may_never_approve():
    """'The system approved it' is not human review."""
    port = _port()
    assert await port.can_approve(
        SYSTEM_PRINCIPAL, capability=CAPABILITY, subject_ref=OPEN_REC
    ) is False


async def test_nobody_approves_what_they_may_not_read():
    port = _port()
    assert await port.can_approve(
        human(ADMIN, WRITE), capability=CAPABILITY, subject_ref=RESTRICTED_REC
    ) is False


async def test_a_write_holder_may_approve_an_unrestricted_run():
    port = _port()
    assert await port.can_approve(
        human(WRITE), capability=CAPABILITY, subject_ref=OPEN_REC
    ) is True


async def test_a_read_only_user_may_not_approve():
    port = _port()
    assert await port.can_approve(
        human(READ), capability=CAPABILITY, subject_ref=OPEN_REC
    ) is False


# ── the restriction predicate itself ────────────────────────────────────────
async def test_is_restricted_reads_the_route():
    factory = _factory()
    async with factory() as session:
        assert await is_restricted(session, RESTRICTED_REC) is True
        assert await is_restricted(session, OPEN_REC) is False
        assert await is_restricted(session, MISSING_REC) is True


async def test_is_restricted_treats_an_error_as_restricted():
    factory = _factory(raises=True)
    async with factory() as session:
        assert await is_restricted(session, OPEN_REC) is True


# ── the principal mapping ───────────────────────────────────────────────────
async def test_claims_become_a_principal():
    p = principal_from_claims({
        "sub": "u-7",
        "scope": f"{READ} {WRITE}",
        "groups": ["g-1", "g-2", ""],
    })
    assert p.user_id == "u-7"
    assert p.scopes == frozenset({READ, WRITE})
    assert p.groups == frozenset({"g-1", "g-2"})
    assert p.service is False
    assert p.is_human is True


async def test_a_token_without_a_subject_is_not_a_human_principal():
    """No fallback to an email or a name: an id from two places is an id two
    tokens can share."""
    p = principal_from_claims({"scope": READ})
    assert p.user_id == ""
    assert p.is_human is False


async def test_empty_claims_produce_no_scopes():
    p = principal_from_claims({})
    assert p.scopes == frozenset()
    assert p.groups == frozenset()


# ── drift guard on the shared vocabulary ────────────────────────────────────
async def test_the_restricted_route_still_matches_what_the_api_filters_on():
    """The taxonomy lives in the agent factory, a separate service.

    `app/api.py` filters alerts on this literal and this module decides access
    on it. If the factory ever renames the route, both must change together --
    and a mismatch would silently make every recording look unrestricted here.
    """
    api_source = (Path(__file__).resolve().parent.parent / "app" / "api.py").read_text(
        encoding="utf-8"
    )
    assert RESTRICTED_ROUTE == "safeguarding_restricted"
    assert (
        f'"{RESTRICTED_ROUTE}"' in api_source
    ), "app/api.py no longer mentions this route; the constants have drifted"
