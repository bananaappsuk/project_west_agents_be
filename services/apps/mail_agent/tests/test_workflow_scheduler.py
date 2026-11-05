"""The background workflow sweep.

Retry and crash recovery are only real if something actually calls them, so this
covers the wiring rather than the engine behaviour (which the library's own suite
tests). The property that matters most here: the sweep runs as the system
principal, so it can drive a workflow up to an approval gate and never through one.
"""

from __future__ import annotations

import pytest

from app import orchestration, scheduler
from app.config import settings


@pytest.fixture
def swept(monkeypatch):
    """Record which sweeps ran, without touching a database."""
    calls: list[str] = []

    class FakeOrchestrator:
        async def recover_stale(self, *, principal):
            calls.append("recover_stale:%s" % ("service" if principal.service else "user"))
            return []

        async def resume_due(self, *, principal):
            calls.append("resume_due:%s" % ("service" if principal.service else "user"))
            return []

        async def resume_policy_gated(self, *, principal, policy, definition=None):
            calls.append(
                "resume_policy_gated:%s:%s:%s"
                % ("service" if principal.service else "user", policy, definition)
            )
            return []

    monkeypatch.setattr(
        orchestration, "build_orchestrator", lambda authorization: FakeOrchestrator()
    )
    return calls


@pytest.mark.asyncio
async def test_the_sweep_runs_recovery_retry_and_the_policy_release(swept):
    await scheduler._workflow_tick()
    assert [c.split(":")[0] for c in swept] == [
        "recover_stale", "resume_due", "resume_policy_gated",
    ]


@pytest.mark.asyncio
async def test_recovery_runs_before_retry(swept):
    """Order matters: recovery reschedules an abandoned run, and the retry sweep
    in the same tick then picks it up rather than leaving it a tick behind."""
    await scheduler._workflow_tick()
    assert swept.index("recover_stale:service") < swept.index("resume_due:service")


@pytest.mark.asyncio
async def test_the_sweep_runs_as_a_service_principal(swept):
    """Which is what the engine refuses the *human* approval path to. A sweep that
    ran as a user would be an automated approval with somebody's name on it."""
    await scheduler._workflow_tick()
    # The actor is the second field; `resume_policy_gated` carries its policy and
    # definition after it, so an endswith check no longer reads every call.
    assert [c.split(":")[1] for c in swept] == ["service"] * len(swept)


@pytest.mark.asyncio
async def test_the_sweep_uses_the_system_authorization_port(monkeypatch):
    seen = {}

    def capture(authorization):
        seen["port"] = authorization

        class Noop:
            async def recover_stale(self, *, principal):
                return []

            async def resume_due(self, *, principal):
                return []

        return Noop()

    monkeypatch.setattr(orchestration, "build_orchestrator", capture)
    await scheduler._workflow_tick()
    assert isinstance(seen["port"], orchestration.SystemAuthorizationPort)


@pytest.mark.asyncio
async def test_a_failing_sweep_does_not_raise(monkeypatch, caplog):
    """An exception escaping here would kill the scheduler job and stop every
    future retry, not just this one."""
    class Exploding:
        async def recover_stale(self, *, principal):
            raise RuntimeError("database unreachable")

        async def resume_due(self, *, principal):
            raise RuntimeError("database unreachable")

    monkeypatch.setattr(
        orchestration, "build_orchestrator", lambda authorization: Exploding()
    )
    await scheduler._workflow_tick()      # must not raise
    assert "workflow sweep FAILED" in caplog.text


def test_the_sweep_is_registered_on_its_own_schedule(monkeypatch):
    """A separate job from the mail sync: a transient send failure must not wait
    on an hourly or daily fetch interval to retry."""
    jobs: list[dict] = []

    monkeypatch.setattr(settings, "cron_enabled", True)
    monkeypatch.setattr(
        scheduler.scheduler, "add_job",
        lambda fn, trigger, **kw: jobs.append({"fn": fn.__name__, **kw}),
    )
    monkeypatch.setattr(scheduler.scheduler, "start", lambda: None)

    scheduler.start()
    ids = {j["id"] for j in jobs}
    assert ids == {"mail_cron_tick", "mail_workflow_tick"}
    workflow_job = next(j for j in jobs if j["id"] == "mail_workflow_tick")
    assert workflow_job["fn"] == "_workflow_tick"
    assert workflow_job["minutes"] == settings.workflow_tick_minutes


def test_the_sweep_respects_the_master_switch(monkeypatch):
    """One background scheduler, one off switch."""
    jobs: list[str] = []
    monkeypatch.setattr(settings, "cron_enabled", False)
    monkeypatch.setattr(
        scheduler.scheduler, "add_job",
        lambda fn, trigger, **kw: jobs.append(kw.get("id", "")),
    )
    scheduler.start()
    assert jobs == []


# ── the third sweep ──────────────────────────────────────────────────────────
#
# Neither `recover_stale` nor `resume_due` reaches a run in `awaiting_approval`.
# That is correct for a run waiting on a person and wrong for one waiting on the
# org's auto-reply setting, where the authority already exists -- so without this
# sweep a process that died between starting an autonomous run and approving it
# would strand that run forever.


@pytest.mark.asyncio
async def test_the_policy_release_is_scoped_to_the_autonomous_definition(swept):
    """The safety property. Scoped to `mail.auto_reply`, so a reply waiting for a
    human reviewer can never be released by a scheduled job."""
    await scheduler._workflow_tick()
    released = [c for c in swept if c.startswith("resume_policy_gated")]
    assert len(released) == 1
    _, actor, policy, definition = released[0].split(":")
    assert actor == "service"
    assert policy == orchestration.AUTO_REPLY_POLICY
    assert definition == orchestration.AUTO_REPLY
    assert definition != orchestration.REPLY_APPROVAL


@pytest.mark.asyncio
async def test_a_failing_policy_release_does_not_kill_the_tick(monkeypatch):
    """Same contract as the other two sweeps: the job must survive to try again."""

    class Exploding:
        async def recover_stale(self, *, principal):
            return []

        async def resume_due(self, *, principal):
            return []

        async def resume_policy_gated(self, *, principal, policy, definition=None):
            raise RuntimeError("the database is unreachable")

    monkeypatch.setattr(
        orchestration, "build_orchestrator", lambda authorization: Exploding()
    )
    # Returns rather than raising; the next tick retries.
    assert await scheduler._workflow_tick() is None
