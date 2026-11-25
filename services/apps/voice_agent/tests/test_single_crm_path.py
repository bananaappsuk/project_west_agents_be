"""One path from a recording to the CRM, and a test that says so.

This is AC-5's E-2 as a standing constraint rather than a one-off observation.
Before the cutover there were two submission paths -- `pipeline._run` and
`POST /recordings/{id}/retry-analysis` -- and each had its own copy of the
thirteen field assignments that follow a submission. Now there is one, inside
`voice.process_intake`, so the approval gate, the retry policy, the lease and
the audit trail apply to every submission rather than to whichever path happened
to be taken.

Source inspection rather than runtime interception, deliberately: a runtime test
can only prove that the paths it exercised did not submit. Reading the modules
proves no other call site exists at all, which is the claim E-2 actually makes.

    .venv/Scripts/python.exe -m pytest tests/test_single_crm_path.py -q
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"

#: The one module allowed to decide a submission, and the one allowed to perform
#: the HTTP call it decides on.
SUBMISSION_DECIDER = "crm_sync.py"
HTTP_CLIENT = "crm_client.py"
#: The only module permitted to call the decider.
ONLY_CALLER = "orchestration.py"

CLIENT_FUNCTIONS = {
    "submit_referral",
    "submit_communication",
    "submit_change_request",
    "submit_activity",
}


def _modules() -> list[Path]:
    return sorted(p for p in APP.glob("*.py") if p.name != "__init__.py")


def _calls(path: Path) -> list[str]:
    """Dotted names of every call in a module, from the parse tree.

    An AST, not a text search: a mention in a docstring or a comment is not a
    call, and this test would be worthless if prose could trip it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            parts = [func.attr]
            inner = func.value
            while isinstance(inner, ast.Attribute):
                parts.append(inner.attr)
                inner = inner.value
            if isinstance(inner, ast.Name):
                parts.append(inner.id)
            found.append(".".join(reversed(parts)))
        elif isinstance(func, ast.Name):
            found.append(func.id)
    return found


def test_only_the_orchestration_adapter_decides_a_submission():
    """`after_call_analysis` has exactly one caller in application code."""
    callers = {
        path.name: [c for c in _calls(path) if c.endswith("after_call_analysis")]
        for path in _modules()
        if path.name != SUBMISSION_DECIDER
    }
    offenders = {name: calls for name, calls in callers.items()
                 if calls and name != ONLY_CALLER}
    assert not offenders, (
        f"these modules call after_call_analysis and should not: {offenders}. "
        "The workflow step is the only submission path (AC-5, E-2)."
    )
    assert callers[ONLY_CALLER], (
        "the orchestration adapter no longer calls after_call_analysis; "
        "the workflow would submit nothing"
    )
    assert len(callers[ONLY_CALLER]) == 1, (
        f"more than one call site in {ONLY_CALLER}: {callers[ONLY_CALLER]}"
    )


def test_only_crm_sync_performs_the_http_call():
    """The intake client is reached through the decider, never around it.

    A module that called `crm_client.submit_referral` directly would skip the
    intent decision, the enum validation and the child sanitising -- the three
    things that stop a malformed referral reaching the CRM.
    """
    offenders: dict[str, list[str]] = {}
    for path in _modules():
        if path.name in (SUBMISSION_DECIDER, HTTP_CLIENT):
            continue
        direct = [
            c for c in _calls(path)
            if c.startswith("crm_client.") and c.split(".")[-1] in CLIENT_FUNCTIONS
        ]
        if direct:
            offenders[path.name] = direct
    assert not offenders, f"these modules call the intake client directly: {offenders}"


def test_the_pipeline_does_not_import_the_submission_modules():
    """Stronger than "does not call": it cannot, without an import.

    The import line is where a second submission path would begin, so this is
    the cheapest place to notice one being added.
    """
    tree = ast.parse((APP / "pipeline.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
    assert "crm_sync" not in imported, (
        "pipeline.py imports crm_sync again; the workflow is the only path"
    )
    assert "crm_client" not in imported


def test_the_api_does_not_import_the_submission_decider():
    tree = ast.parse((APP / "api.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(a.name for a in node.names)
    assert "crm_sync" not in imported
    assert "crm_client" not in imported


def test_the_retry_endpoint_goes_through_the_workflow():
    """It used to analyse and submit inline; now it starts a run.

    `start_retry` is what allocates a distinct run under the advisory lock, so
    this also pins the Q-2 behaviour to the endpoint that needs it.
    """
    source = (APP / "api.py").read_text(encoding="utf-8")
    assert "start_retry" in source
    assert "ALREADY_RUNNING" in source
    assert "status.HTTP_409_CONFLICT" in source, (
        "the 409 for a concurrent retry must be preserved"
    )


def test_the_upload_endpoint_still_does_not_submit():
    """Pre-existing behaviour, preserved rather than tidied.

    `POST /recordings/upload` analyses an uploaded file and has never submitted
    it to the CRM. That asymmetry is not this change's business, and quietly
    "fixing" it would start sending referrals that were never sent before.
    """
    source = (APP / "api.py").read_text(encoding="utf-8")
    assert "agent_client.analyze_call" in source, "upload still analyses"
    calls = [c for c in _calls(APP / "api.py") if c.endswith("after_call_analysis")]
    assert not calls, "the upload path must not have gained a submission"
