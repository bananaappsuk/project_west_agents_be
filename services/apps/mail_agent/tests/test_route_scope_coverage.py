"""Every email route must be mailbox-scoped.

This is the regression guard the SSOT asks for by name. The dangerous failure here
is not a wrong filter — it is a NEW route added later that nobody remembers to scope,
which would return every mailbox's mail to every user and raise no error at all.

The test enumerates the real router rather than a list someone maintains by hand, so
a forgotten route fails the build instead of shipping.
"""

from __future__ import annotations

import inspect

import pytest

from app.api import router
from app.deps import mailbox_scope

#: Routes that legitimately take no mailbox scope, each with the reason it is safe.
#: Adding to this set is a deliberate act and should be argued for in review.
UNSCOPED_ALLOWED = {
    "/health",                  # liveness only, returns no data
    "/folders",                 # folder names, carry no mail
    "/folders/{folder_id}",
    "/settings/agent",          # scheduler config, not mail
    "/settings/mailbox",        # system config; being removed with the Settings form
    "/settings/mailbox/test",
    "/agent/status",            # run counters, no message content
}


def _email_routes():
    """Routes that can reach email rows."""
    for r in router.routes:
        path = getattr(r, "path", "")
        if not path.startswith("/emails"):
            continue
        yield path, r


def _depends_on_scope(endpoint) -> bool:
    for param in inspect.signature(endpoint).parameters.values():
        default = param.default
        dep = getattr(default, "dependency", None)
        if dep is mailbox_scope:
            return True
    return False


def test_every_email_route_depends_on_mailbox_scope():
    missing = [
        path for path, r in _email_routes()
        if path not in UNSCOPED_ALLOWED and not _depends_on_scope(r.endpoint)
    ]
    assert not missing, (
        "these email routes have no mailbox scope and would expose every mailbox "
        f"to every user: {sorted(set(missing))}"
    )


def test_there_are_email_routes_to_check():
    """Guards the guard: a refactor that renames the prefix must not silently
    turn this suite into a no-op that passes because it found nothing."""
    assert list(_email_routes()), "no /emails routes found — has the prefix changed?"


def test_unscoped_allowlist_contains_no_email_routes():
    """Nothing under /emails may be waved through on the allowlist."""
    leaked = [p for p in UNSCOPED_ALLOWED if p.startswith("/emails")]
    assert not leaked, f"email routes must never be allowlisted: {leaked}"


@pytest.mark.parametrize("path", ["/emails", "/emails/{email_id}/reply"])
def test_known_sensitive_routes_are_scoped(path):
    """Belt and braces on the two that matter most: the list, and the send."""
    for p, r in _email_routes():
        if p == path:
            assert _depends_on_scope(r.endpoint), f"{path} is not scoped"
            return
    pytest.fail(f"route {path} not found")
