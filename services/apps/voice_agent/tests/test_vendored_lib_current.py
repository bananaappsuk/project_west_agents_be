"""The vendored orchestration library must match `libs/`, and must import.

This service deploys from its own directory, so the shared package has to be
physically present inside it -- the same convention `platform_common` already
follows here, and the reason `requirements.txt` must not name the library as a
path dependency: the deploy's build root does not contain `libs/`.

A copy can drift, and drift has already happened in this repo:
`services/auth/platform_common/db.py` is not the same file as the other four
copies of it. `DECISIONS.md` D8 records that consequence as accepted precisely
because `scripts/sync_shared_libs.py` makes the copy reproducible and a test
fails the build when it is stale. This is that test for the voice service; the
mail agent's equivalent lives in `tests/test_workflow_route_scope.py`.

A stale copy is not a cosmetic problem. It means this service runs orchestration
code that no longer exists upstream -- a lifecycle, approval or retry rule that
was corrected in `libs/` and never reached here.
"""

from __future__ import annotations

import filecmp
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
VENDORED = SERVICE_DIR / "platform_orchestration"
CANONICAL = (
    SERVICE_DIR.parent.parent.parent
    / "libs"
    / "platform_orchestration"
    / "platform_orchestration"
)

SYNC = "python scripts/sync_shared_libs.py"


def _modules(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    }


def test_the_canonical_library_is_where_this_test_expects_it():
    """Guards the path arithmetic above.

    Without this, moving either directory turns every assertion below into a
    comparison of two empty sets, which passes.
    """
    assert CANONICAL.is_dir(), "canonical library not found at %s" % CANONICAL
    assert VENDORED.is_dir(), (
        "no vendored copy at %s -- run %s" % (VENDORED, SYNC)
    )
    assert _modules(CANONICAL), "the canonical library has no modules; path is wrong"


def test_the_vendored_copy_holds_the_same_modules():
    vendored, canonical = _modules(VENDORED), _modules(CANONICAL)

    missing = sorted(canonical - vendored)
    extra = sorted(vendored - canonical)

    assert not missing, (
        "these modules exist in libs/ and not in the vendored copy: %s -- run %s"
        % (missing, SYNC)
    )
    # Extra matters as much as missing: a module deleted upstream but left here
    # stays importable, and an import will find it.
    assert not extra, (
        "these modules exist in the vendored copy and not in libs/: %s -- run %s"
        % (extra, SYNC)
    )


def test_every_vendored_module_is_byte_identical_to_libs():
    differing = [
        rel
        for rel in sorted(_modules(CANONICAL))
        if not filecmp.cmp(CANONICAL / rel, VENDORED / rel, shallow=False)
    ]
    assert not differing, (
        "these vendored modules differ from libs/: %s -- run %s" % (differing, SYNC)
    )


def test_the_vendored_copy_is_what_this_service_imports():
    """Matching files are not enough: the import has to resolve to the copy.

    `pytest.ini` puts the service directory on the path, which is what the
    deployed service does too. If an installed distribution ever shadowed the
    vendored package, every assertion above could pass while the service ran
    something else entirely.
    """
    import platform_orchestration

    resolved = Path(platform_orchestration.__file__).resolve().parent
    assert resolved == VENDORED, (
        "platform_orchestration resolved to %s, not the vendored copy at %s"
        % (resolved, VENDORED)
    )


def test_the_vendored_copy_exposes_the_api_the_adapter_will_use():
    """A smoke test over the import, not a test of the library.

    The library has its own suite under `libs/platform_orchestration/tests`. This
    only checks that the names the voice adapter is planned to use are importable
    from the copy, so a truncated or partially-synced copy fails here rather than
    at service startup.
    """
    import platform_orchestration as po

    for name in (
        "Orchestrator",
        "WorkflowDefinition",
        "StepSpec",
        "StepResult",
        "DefinitionRegistry",
        "HandlerRegistry",
        "Principal",
        "AuthorizationPort",
        "SYSTEM_PRINCIPAL",
        "WorkflowState",
        "InvalidTransition",
        "NotAuthorized",
    ):
        assert hasattr(po, name), "the vendored copy does not export %r" % name


def test_the_sql_repository_is_importable_separately():
    """`platform_orchestration.sql` is deliberately not imported by the package.

    Its docstring says so: a host that does not persist workflows, or a test
    suite, never needs SQLAlchemy to import the package. The voice service does
    persist them, so it will import this module -- and a copy missing it would
    only fail once the adapter was wired.
    """
    from platform_orchestration.sql import SqlAuditSink, SqlWorkflowRepository

    assert SqlWorkflowRepository is not None
    assert SqlAuditSink is not None
