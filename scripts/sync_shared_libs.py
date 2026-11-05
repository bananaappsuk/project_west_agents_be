"""Copy the canonical shared libraries in `libs/` into each service that uses them.

Every service in this monorepo deploys from its own directory, so a shared package
has to be physically present inside it -- which is why `platform_common` already
exists as a tracked copy under each service alongside the canonical source in
`libs/`. Those copies were made by hand and have drifted (`services/auth`'s `db.py`
differs from the others'), which is the failure this script exists to stop.

Run it after changing anything in `libs/`:

    python scripts/sync_shared_libs.py            # copy, reporting what changed
    python scripts/sync_shared_libs.py --check    # verify only; non-zero if stale

`--check` is the form for CI and for the drift test in each service's suite: it
makes a stale copy fail a build rather than ship.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

#: package name -> the service directories that vendor it.
#:
#: `platform_common` is deliberately NOT listed. Its copies already differ from
#: each other (`services/auth/platform_common/db.py` is not the same file as the
#: others'), and reconciling them is a change to four running services with its
#: own testing -- not something to do as a side effect of syncing a different
#: package. Add it here once those copies have been reviewed and squared up.
VENDORED: dict[str, tuple[str, ...]] = {
    "platform_orchestration": (
        "services/apps/mail_agent",
    ),
}

#: Never copied: build artefacts and caches, which are per-environment.
IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", "*.egg-info", ".pytest_cache", ".mypy_cache",
)


def _source_for(package: str) -> Path:
    # The canonical copy lives at libs/<dist-name>/<package>/, where the
    # distribution directory uses the package name as-is in this repo.
    return BACKEND / "libs" / package / package


def _differences(src: Path, dst: Path) -> list[str]:
    """Relative paths that differ, are missing, or are extra."""
    out: list[str] = []
    src_files = {
        p.relative_to(src).as_posix()
        for p in src.rglob("*.py")
        if "__pycache__" not in p.parts
    }
    dst_files = {
        p.relative_to(dst).as_posix()
        for p in dst.rglob("*.py")
        if "__pycache__" not in p.parts
    } if dst.exists() else set()

    for rel in sorted(src_files - dst_files):
        out.append("missing: %s" % rel)
    for rel in sorted(dst_files - src_files):
        out.append("extra:   %s" % rel)
    for rel in sorted(src_files & dst_files):
        if not filecmp.cmp(src / rel, dst / rel, shallow=False):
            out.append("differs: %s" % rel)
    return out


def check() -> int:
    stale = 0
    for package, services in VENDORED.items():
        src = _source_for(package)
        if not src.is_dir():
            print("ERROR: no canonical source at %s" % src)
            return 2
        for service in services:
            dst = BACKEND / service / package
            diffs = _differences(src, dst)
            if diffs:
                stale += 1
                print("STALE %s/%s" % (service, package))
                for line in diffs:
                    print("    %s" % line)
            else:
                print("ok    %s/%s" % (service, package))
    if stale:
        print(
            "\n%d vendored copy/copies are stale. "
            "Run: python scripts/sync_shared_libs.py" % stale
        )
    return 1 if stale else 0


def sync() -> int:
    for package, services in VENDORED.items():
        src = _source_for(package)
        if not src.is_dir():
            print("ERROR: no canonical source at %s" % src)
            return 2
        for service in services:
            dst = BACKEND / service / package
            had_changes = bool(_differences(src, dst))
            if dst.exists():
                # Removed wholesale rather than merged: a file deleted from the
                # canonical package must disappear from the copy, and an
                # incremental copy is exactly the shape of bug that leaves a
                # stale module behind for an import to find.
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=IGNORE)
            print("%s %s/%s" % ("synced" if had_changes else "ok    ", service, package))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="verify the copies match; exit non-zero if any is stale",
    )
    args = parser.parse_args()
    return check() if args.check else sync()


if __name__ == "__main__":
    sys.exit(main())
