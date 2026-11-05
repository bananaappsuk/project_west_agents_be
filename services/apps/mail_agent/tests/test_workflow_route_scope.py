"""Every workflow route must be mailbox-scoped, and the vendored library must not drift.

The same regression guard `test_route_scope_coverage.py` applies to the email
routes, applied to the workflow router. A new orchestration route is exactly the
kind of thing that gets added later without a scope dependency: it reads workflow
rows rather than Email rows, so it does not *look* like it touches mail, but every
one of them resolves to an email through `subject_ref` and would expose one
mailbox's review queue to another mailbox's users.

A separate guard rather than an extension of the existing one on purpose: that
helper identifies routes by whether they name `Email`, which these do not, and
loosening it to catch them would weaken the test that currently protects the email
routes.
"""

from __future__ import annotations

import filecmp
import inspect
from pathlib import Path

import pytest

from app.deps import mailbox_scope
from app.workflow_api import router

#: Workflow routes that legitimately take no mailbox scope. Empty, and that is
#: the point: every one of these routes reaches an email through its run's
#: `subject_ref`, so there is no such thing as a workflow route that does not need
#: scoping. Adding to this set is a deliberate act and should be argued for.
UNSCOPED_ALLOWED: set[str] = set()


def _routes():
    for r in router.routes:
        if getattr(r, "endpoint", None) is None:
            continue
        yield getattr(r, "path", ""), r


def _depends_on_scope(endpoint) -> bool:
    for param in inspect.signature(endpoint).parameters.values():
        dep = getattr(param.default, "dependency", None)
        if dep is mailbox_scope:
            return True
    return False


def _requires_a_scope_check(endpoint) -> bool:
    """Whether the handler puts its scope through the authorization port.

    Depending on `mailbox_scope` is not sufficient on its own -- a route could take
    the scope and never use it. Every one of these handlers must hand it to the
    orchestrator, which is what actually enforces the grant.

    Two spellings count, because a route may build the orchestrator itself or
    delegate to the shared helper in `orchestration.py` that builds it. Both end
    at `MailboxAuthorizationPort(scope)`; what the guard is really checking is that
    the scope the route resolved is the scope the decision is made against, rather
    than a parameter the handler accepted and dropped.
    """
    try:
        source = inspect.getsource(endpoint)
    except (OSError, TypeError):  # pragma: no cover - builtins, C callables
        return False
    return "_orchestrator_for(scope)" in source or "scope=scope" in source


def test_there_are_workflow_routes_to_check():
    """Guards the guard: a rename must not turn this into a no-op that passes
    because it found nothing."""
    assert list(_routes()), "no workflow routes found - has the router moved?"


def test_every_workflow_route_depends_on_mailbox_scope():
    missing = [
        path for path, r in _routes()
        if path not in UNSCOPED_ALLOWED and not _depends_on_scope(r.endpoint)
    ]
    assert not missing, (
        "these workflow routes have no mailbox scope and would expose every "
        "mailbox's workflows to every user: %s" % sorted(set(missing))
    )


def test_every_workflow_route_passes_its_scope_to_the_orchestrator():
    unused = [
        path for path, r in _routes()
        if path not in UNSCOPED_ALLOWED and not _requires_a_scope_check(r.endpoint)
    ]
    assert not unused, (
        "these workflow routes take a mailbox scope but never hand it to the "
        "orchestrator, so nothing enforces it: %s" % sorted(set(unused))
    )


def test_the_allowlist_is_empty():
    """Stated as a test so growing it is a visible decision."""
    assert UNSCOPED_ALLOWED == set()


@pytest.mark.parametrize(
    "path",
    [
        "/workflows/reply-approval",   # starting a send
        "/workflows/review-queue",     # listing other people's pending reviews
        "/workflows/{run_id}/approve",  # authorising a send
    ],
)
def test_the_routes_that_matter_most_are_scoped(path):
    for p, r in _routes():
        if p == path:
            assert _depends_on_scope(r.endpoint), "%s is not scoped" % path
            return
    pytest.fail("route %s not found" % path)


# ── the vendored copy must match the canonical library ──────────────────────

def test_the_vendored_orchestration_library_matches_libs():
    """This service deploys from its own directory, so it carries a copy of the
    shared package -- the same convention `platform_common` already follows.

    A stale copy means the service runs code that no longer exists upstream, which
    is the failure `platform_common`'s copies have already suffered (auth's `db.py`
    differs from the others'). `scripts/sync_shared_libs.py` refreshes it.
    """
    service_dir = Path(__file__).resolve().parent.parent
    vendored = service_dir / "platform_orchestration"
    canonical = (
        service_dir.parent.parent.parent
        / "libs" / "platform_orchestration" / "platform_orchestration"
    )
    assert canonical.is_dir(), "canonical library not found at %s" % canonical

    def modules(root: Path) -> set[str]:
        return {
            p.relative_to(root).as_posix()
            for p in root.rglob("*.py")
            if "__pycache__" not in p.parts
        }

    assert modules(vendored) == modules(canonical), (
        "the vendored copy and libs/ hold different modules; run "
        "python scripts/sync_shared_libs.py"
    )

    differing = [
        rel for rel in sorted(modules(canonical))
        if not filecmp.cmp(canonical / rel, vendored / rel, shallow=False)
    ]
    assert not differing, (
        "these vendored modules differ from libs/: %s - run "
        "python scripts/sync_shared_libs.py" % differing
    )
